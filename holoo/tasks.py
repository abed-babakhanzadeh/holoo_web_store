import logging
from datetime import timedelta
from celery import shared_task
from django.apps import apps
from django.utils import timezone
from django.utils.text import slugify

from .client import HolooClient
from .invoice import build_invoice_payload, item_lines, payload_total
from .locks import task_lock
from .product_state import apply_holoo_product_state, row_values

logger = logging.getLogger(__name__)

# اگر بیش از این مدت از ثبت سفارش گذشته و هنوز فاکتورش در هلو ثبت نشده، یک‌بار به مدیر خبر می‌دهیم
HOLOO_SYNC_STALL_THRESHOLD = timedelta(days=3)

# تسک بازبینی فقط سراغ سفارش‌هایی می‌رود که از این مدت بیشتر گذشته باشد، تا با تلاش‌های
# در جریانِ چرخه‌ی عادی تداخل نکند (قفل هم هست، این فقط سر و صدای اضافه را کم می‌کند)
HOLOO_RECONCILE_MIN_AGE = timedelta(minutes=30)


def _alert_admin_if_holoo_sync_stalled(order, what='ثبت فاکتور'):
    """
    وقتی همگام‌سازی سفارش با هلو بیش از HOLOO_SYNC_STALL_THRESHOLD طول بکشد (مثلاً قطعی
    طولانی شبکه/هلو)، یک‌بار (نه در هر retry) لاگ بحرانی + پیامک به مدیر می‌فرستد تا در
    صورت نیاز پیگیری دستی کند. تلاش خودکار پس‌زمینه همچنان ادامه دارد؛ این فقط برای اطلاع است.
    """
    if order.holoo_sync_alert_sent:
        return
    if timezone.now() - order.created_at < HOLOO_SYNC_STALL_THRESHOLD:
        return

    from notifications.service import notify_admin

    days = HOLOO_SYNC_STALL_THRESHOLD.days
    logger.critical("سفارش %s بیش از %s روز است %s آن در هلو انجام نشده؛ نیاز به بررسی دستی دارد.", order.id, days, what)
    notify_admin('holoo_sync_stalled_admin', order_id=order.id, days=days, what=what)

    order.holoo_sync_alert_sent = True
    order.save(update_fields=['holoo_sync_alert_sent'])

def _writes_disabled():
    """ HOLOO_WRITE_MODE=disabled: هیچ تسک نوشتنی نباید اجرا یا retry شود؛ سفارش/کاربر برای زمان فعال شدن دست‌نخورده می‌ماند """
    from .conf import get_config
    return get_config().write_is_disabled


def _holoo_address(user):
    """ آدرس مشتری در هلو = آدرس پیش‌فرض کاربر (استان، شهر، ناحیه، آدرس)؛ کاربر بدون آدرس ← رشته‌ی خالی """
    address = user.default_address
    return address.full_text if address else ''


def _customer_extra(user):
    """ فیلدهای آدرسِ مشتری برای هلو از آدرس پیش‌فرض کاربر: متن کامل، استان، شهر، کدپستی (بدون آدرس ← خالی) """
    address = user.default_address
    if address is None:
        return {'address': '', 'province': '', 'city': '', 'postal_code': ''}
    return {'address': address.full_text, 'province': address.city.province.name, 'city': address.city.name,
            'postal_code': address.postal_code or ''}


def _blank(value):
    return str(value or '').strip().lower() in ('', 'null', 'none')


def fill_blank_update(row, update, *, client_id):
    """
    آنچه سایت می‌تواند روی مشتریِ قدیمیِ هلو بنویسد: فقط فیلدهایی که در هلو خالی‌اند (کدملی، موبایل، آدرس/استان/شهر/کدپستی)؛
    نام هرگز. {} یعنی چیزی برای نوشتن نیست. اگر مشتری WebIdِ دیگری دارد هم {} برمی‌گردد: PUT بدون همان id آن را پاک می‌کند و
    WebId فقط وقتی خالی است یا از همین کاربر است (client_id) با PUT نوشته می‌شود.
    """
    web = row.get('WebId')
    if not _blank(web) and str(web) != str(client_id):
        return {}
    out = {}
    if _blank(row.get('NationalId')) and update.get('national_code'):
        out['national_code'] = update['national_code']
    if _blank(row.get('Mobile')) and update.get('phone_number'):
        out['phone_number'] = update['phone_number']
    if _blank(row.get('Address')) and update.get('address'):
        out['address'] = update['address']
        for key, holoo_key in (('province', 'Ostan'), ('city', 'City'), ('postal_code', 'ZipCode')):
            if update.get(key) and _blank(row.get(holoo_key)):
                out[key] = update[key]
    return out


def sync_customer(user, client):
    """
    ثبت (یا ویرایش) مشتری در هلو و ذخیره‌ی ErpCode، کد طرف‌حساب و سرفصل بدهکار روی کاربر. خروجی: دیکشنری client.
    هم تسک sync_user_to_holoo و هم ثبت فاکتور (وقتی مشتری هنوز در هلو نیست) از همین تابع استفاده می‌کنند.
    """
    from accounts.models import UserStatus

    extra = _customer_extra(user)
    if user.erp_code:
        # کاربر قبلا در هلو بوده، پس فقط باید آپدیت شود
        logger.info(f"شروع آپدیت کاربر {user.phone_number} در هلو...")
        update = dict(
            first_name=user.first_name, last_name=user.last_name, address=extra['address'],
            phone_number=user.phone_number, national_code=user.national_code,
            province=extra['province'], city=extra['city'], postal_code=extra['postal_code'],
        )
        if user.imported_from_holoo and not client.config.write_is_mock:
            # مشتریِ قدیمیِ هلو: هلو مالک نام/آدرس/موبایل اوست؛ سایت فقط جاهای خالیِ هلو را پر می‌کند
            row = client._lookup_customer(erpcode=user.erp_code)
            if row is None:
                result = {"success": False, "transient": True, "message": "مشتری در هلو خوانده نشد؛ دوباره تلاش می‌شود."}
                update = None
            else:
                update = fill_blank_update(row, update, client_id=client._client_id(user.id))
        if update is None:
            pass
        elif not update:
            result = {"success": True, "message": "در هلو چیزی برای تکمیل نبود."}
        else:
            result = client.update_person(erp_code=user.erp_code, web_id=user.id, **update)
    else:
        # مشتری جدید است، باید ساخته شود
        logger.info(f"شروع ثبت مشتری جدید {user.phone_number} در هلو...")
        result = client.insert_person(
            first_name=user.first_name,
            last_name=user.last_name,
            phone_number=user.phone_number,
            national_code=user.national_code,
            address=extra['address'],
            web_id=user.id, province=extra['province'], city=extra['city'], postal_code=extra['postal_code'],
        )

    if result.get('success'):
        fields = ['status', 'last_sync_error', 'retry_count']
        if not user.erp_code:
            user.erp_code = result.get('erp_code')
            fields.append('erp_code')
        for attr, key in (('holoo_customer_code', 'code'), ('holoo_bed_sarfasl', 'bed_sarfasl')):
            if result.get(key):
                setattr(user, attr, str(result[key]))
                fields.append(attr)
        user.status = UserStatus.ACTIVE
        user.last_sync_error = None
        user.retry_count = 0
        user.save(update_fields=fields)
    return result


# max_retries=10 یعنی تا 10 بار تلاش میکنه (طی چند روز!)
@shared_task(bind=True, max_retries=10)
def sync_user_to_holoo(self, user_id):
    CustomUser = apps.get_model('accounts', 'CustomUser')

    if _writes_disabled():
        return "Skipped (holoo writes disabled)"

    try:
        user = CustomUser.objects.get(id=user_id)
    except CustomUser.DoesNotExist:
        return "User not found."

    result = sync_customer(user, HolooClient())

    if result.get('success'):
        return "Sync Success"

    error_msg = result.get('message', 'خطای نامشخص هلو')
    error_code = result.get('code')
    user.last_sync_error = error_msg
    user.retry_count += 1
    user.save(update_fields=['last_sync_error', 'retry_count'])

    # ---------------------------------------------------------
    # پوکایوکه ۲: توقف تلاش برای خطاهای دائمیِ داده (مثل نام خالی، کدپستی نامعتبر). تکراری بودن موبایل/کدملی (۱۰/۲۳)
    # دیگر خطا نیست: کلاینت مشتریِ موجود در هلو را می‌پذیرد. اگر کلاینت صریحاً transient نداده، کدهای تکراری قدیمی فاتال‌اند.
    # ---------------------------------------------------------
    transient = result.get('transient', str(error_code) not in ('23', '10', '8'))
    if not transient:
        logger.error(f"خطای دیتایی غیرقابل حل مشتری {user.phone_number}: {error_msg}. توقف تلاش.")
        # وضعیت کاربر دست‌نخورده می‌ماند تا داده اصلاح شود
        return "Fatal Data Error - No Retry"

    # ---------------------------------------------------------
    # پوکایوکه ۳: تلاش مجدد تصاعدی برای خطاهای شبکه (Exponential Backoff)
    # ---------------------------------------------------------
    # فرمول: (تعداد دفعات تلاش ^ 2) * 60 ثانیه
    # دفعه اول: 1 دقیقه، دفعه دوم: 4 دقیقه، دفعه سوم: 9 دقیقه، دفعه پنجم: 25 دقیقه و ...
    backoff_time = (self.request.retries ** 2) * 60
    logger.warning(f"خطای ارتباطی: {error_msg}. تلاش مجدد در {backoff_time} ثانیه دیگر...")

    raise self.retry(countdown=backoff_time)


PRODUCT_SYNC_PAGE_SIZE = 500
PRODUCT_SYNC_MAX_PAGES = 100  # سقف ایمنی (۵۰ هزار کالا با اندازه صفحه فعلی)
PRODUCT_SYNC_COUNT_TOLERANCE = 5  # اختلاف مجاز بین تعداد واکشی‌شده و /Product/count برای اجازه دادن به پاک‌سازی

# کالاهایی که «000» بلافاصله کنار «/» در نامشان باشد (چه در ابتدا مثل «000/چوب بستنی» و
# چه در وسط/انتها مثل «... مصرف کننده 699/000») اصلاً روی سایت نمایش داده نمی‌شوند؛ نه
# ساخته می‌شوند و نه آپدیت، طبق تصمیم کارفرما
PRODUCT_NAME_EXCLUDE_PATTERNS = ('000/', '/000')


def is_service_item(item):
    """ کالای خدماتی هلو (مثل «سرويس»/کرایه/پیک): در هلو service=true دارد و نباید وارد فروشگاه عمومی شود """
    value = item.get('service')
    if isinstance(value, str):
        return value.strip().lower() in ('true', '1', 'yes')
    return bool(value)


def classify_item(item):
    """
    وضعیت یک ردیف کالای هلو برای سینک:
      'ok'            ← وارد سایت می‌شود
      'no_erp'        ← ErpCode ندارد
      'excluded_name' ← الگوی نام «000/»
      'inactive'      ← IsActive=False
      'service'       ← کالای خدماتی
    ترتیب بررسی دقیقاً همان ترتیب قبلیِ سینک است (service پس از inactive).
    """
    if not item.get('ErpCode'):
        return 'no_erp'
    name = item.get('Name') or item.get('ErpCode')
    if any(pattern in name for pattern in PRODUCT_NAME_EXCLUDE_PATTERNS):
        return 'excluded_name'
    if not bool(item.get('IsActive', True)):
        return 'inactive'
    if is_service_item(item):
        return 'service'
    return 'ok'


def _safe_float(value, default=0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _erp_slug_suffix(erp_code):
    """
    نسخه‌ی slug-friendly کامل erp_code (نه فقط چند کاراکتر آخر). erp_codeهای هلو معمولاً
    پیشوند/پسوند مشترک زیادی دارند (چون ساختاریافته‌اند)، پس بریدن به ۴-۸ کاراکتر آخر
    می‌تواند بین دو erp_code متفاوت تصادفاً یکسان شود و باعث خطای unique روی slug بشود
    (دقیقاً همین اتفاق برای چند SideGroup مختلف با نام‌های متفاوت افتاد). با نگه‌داشتن کل
    erp_code (فقط حذف کاراکترهای غیرمجاز در slug مثل =) یکتایی تضمینی می‌شود، چون خود
    erp_code در دیتابیس unique است.
    """
    import re
    return re.sub(r'[^a-zA-Z0-9]+', '', erp_code).lower()


def _unique_product_slug(name, erp_code):
    """ چون erp_code یکتاست، اسلاگ با پسوند آن همیشه یکتا خواهد بود؛ نیازی به حلقه‌ی retry نیست """
    base = slugify(name, allow_unicode=True) or 'product'
    suffix = f"-{_erp_slug_suffix(erp_code)}"
    return base[:255 - len(suffix)] + suffix


def _unique_category_slug(name, erp_code, max_length=200):
    base = slugify(name, allow_unicode=True) or 'category'
    suffix = f"-{_erp_slug_suffix(erp_code)}"
    return base[:max_length - len(suffix)] + suffix


@shared_task(bind=True, max_retries=3)
def sync_products_from_holoo(self):
    """
    تسک دوره‌ای Read-only: محصولات و گروه/زیرگروه‌ها را از هلو می‌خواند و در سایت می‌نشاند.
    محصول هرگز به هلو نوشته نمی‌شود؛ محصول فقط در هلو ساخته می‌شود.

    - دسته‌بندی (Category) با get_or_create روی erp_code ساخته می‌شود و در سینک‌های بعدی
      دست‌نخورده می‌ماند (منطق قبلی، بدون تغییر).
    - دسته‌بندیِ محصول (Product.category) فقط در اولین ساخت محصول ست می‌شود؛ اگر ادمین بعداً
      دستی عوض کند، سینک‌های بعدی آن را برنمی‌گردانند.
    - کالاهای IsActive=False هلو اصلاً در دیتابیس سایت ساخته/آپدیت نمی‌شوند (دقیقاً هم‌الگوی
      فیلتر نام PRODUCT_NAME_EXCLUDE_PATTERNS).
    - در پایان، اگر کل کاتالوگ با موفقیت و بدون خطا واکشی شده باشد (بر اساس مقایسه با
      /Product/count)، محصولاتی که دیگر در فهرست هلو نیستند یا غیرفعال شده‌اند، به‌صورت فیزیکی
      از دیتابیس سایت حذف می‌شوند (products.services.delete_products_safely - سفارش‌های قدیمی
      حذف نمی‌شوند، فقط OrderItem.product آن‌ها Null می‌شود). اگر واکشی ناقص بود، این مرحله رد
      می‌شود تا داده‌ای به‌اشتباه از دست نرود.
    """
    # قفل توزیع‌شده: این تسک هر ۲۵ دقیقه شلیک می‌شود، ولی برای کاتالوگ چندهزارتایی می‌تواند
    # بیشتر طول بکشد. بدون قفل، اجرای بعدی روی اجرای قبلی می‌افتد و دو Worker هم‌زمان روی
    # get_or_create دسته‌بندی‌ها (erp_code یکتا) به IntegrityError می‌خورند.
    # timeout کمی بیشتر از فاصله‌ی زمان‌بندی است تا قفل مرده باقی نماند.
    with task_lock('holoo:product_sync', timeout=3600) as acquired:
        if not acquired:
            logger.warning("سینک محصولات هلو هنوز در حال اجراست؛ این اجرا رد شد (جلوگیری از اجرای هم‌زمان).")
            return "Skipped (locked)"
        return _run_product_sync(self)


def _run_product_sync(self):
    Category = apps.get_model('products', 'Category')
    Product = apps.get_model('products', 'Product')
    client = HolooClient()

    try:
        reported_count = client.get_product_count()
        logger.info(f"هلو گزارش می‌دهد مجموعاً {reported_count} کالا دارد.")

        fetched_erp_codes = set()
        created_count = updated_count = error_count = excluded_count = inactive_count = service_count = 0
        fetch_failed = False
        back_in_stock_ids = []
        page = 1

        while page <= PRODUCT_SYNC_MAX_PAGES:
            observed_at = timezone.now()     # لحظه‌ی *شروع* واکشی؛ مبنای stock_synced_at و آزادسازی رزرو
            data = client.get_products(page=page, items_per_page=PRODUCT_SYNC_PAGE_SIZE)
            if data is None:
                logger.error(f"واکشی صفحه {page} از هلو ناموفق بود؛ ادامه بدون مرحله پاک‌سازی.")
                fetch_failed = True
                break

            items = data.get('product', [])
            if not items:
                break

            for item in items:
                try:
                    erp_code = item.get('ErpCode')
                    name = item.get('Name') or erp_code
                    verdict = classify_item(item)
                    if verdict == 'no_erp':
                        logger.warning(f"کالای بدون ErpCode رد شد: {item.get('Name')}")
                        continue
                    # سه دسته‌ی زیر اصلاً وارد سایت نمی‌شوند؛ چون erp_code‌شان به fetched_erp_codes اضافه نمی‌شود،
                    # اگر قبلاً روی سایت بودند مرحله‌ی پاک‌سازی پایین همین تابع خودکار حذفشان می‌کند:
                    #   «مصرف‌کننده/000» (فیلتر نام)، غیرفعال هلو (IsActive=False)، و کالای خدماتی (service=true؛
                    #   مثل کرایه/پیک که فقط ردیف فاکتور است و نباید در فروشگاه عمومی دیده شود)
                    if verdict == 'excluded_name':
                        excluded_count += 1
                        continue
                    if verdict == 'inactive':
                        inactive_count += 1
                        continue
                    if verdict == 'service':
                        service_count += 1
                        continue

                    fetched_erp_codes.add(erp_code)

                    # --- تعیین/ساخت گروه اصلی و زیرگروه (فقط اگر موجود نبود ساخته می‌شود) ---
                    main_group_erp = item.get('MainGroupErpCode')
                    side_group_erp = item.get('SideGroupErpCode')
                    category_to_assign = None

                    if main_group_erp:
                        main_group_name = item.get('MainGroupName') or 'بدون گروه اصلی'
                        main_category, _ = Category.objects.get_or_create(
                            erp_code=main_group_erp,
                            defaults={
                                'name': main_group_name,
                                'slug': _unique_category_slug(main_group_name, main_group_erp),
                                'parent': None,
                            }
                        )
                        category_to_assign = main_category

                        if side_group_erp:
                            side_group_name = item.get('SideGroupName') or 'بدون گروه فرعی'
                            side_category, _ = Category.objects.get_or_create(
                                erp_code=side_group_erp,
                                defaults={
                                    'name': side_group_name,
                                    'slug': _unique_category_slug(side_group_name, side_group_erp),
                                    'parent': main_category,
                                }
                            )
                            category_to_assign = side_category

                    # --- فیلدهای مالی/انبار (فقط ستون‌های مالکِ هلو؛ holoo/product_state.py) ---
                    values = row_values(item)
                    product_code = values['product_code']
                    stock = values['stock']

                    # is_active همیشه True است: کالاهای IsActive=False هلو بالاتر رد شده‌اند و
                    # اصلاً به این نقطه نمی‌رسند (فیلتر «اصلاً وارد دیتابیس نشوند»)
                    product, created = Product.objects.get_or_create(
                        erp_code=erp_code,
                        defaults={
                            'name': name,
                            'slug': _unique_product_slug(name, erp_code),
                            'category': category_to_assign,  # فقط این‌جا، در لحظه‌ی ساخت، ست می‌شود
                            'is_active': True,
                            'stock_synced_at': observed_at,
                            'price_synced_at': observed_at,
                            **values,
                        }
                    )

                    if created:
                        created_count += 1
                    else:
                        # category و slug عمداً دست‌نخورده می‌مانند (تصمیم ادمین/URL محصول حفظ می‌شود). نوشتن فقط از
                        # راه نویسنده‌ی واحد و با update_fields؛ reserved_quantity (مالک: رزرو سفارش‌های سایت) و
                        # داده‌ی جدیدتر هرگز بازنویسی نمی‌شوند.
                        was_out_of_stock = product.stock <= 0
                        if apply_holoo_product_state(product, item, observed_at):
                            updated_count += 1
                            if was_out_of_stock and stock > 0:
                                back_in_stock_ids.append(product.id)

                except Exception as e:
                    error_count += 1
                    logger.error(f"خطا در همگام‌سازی کالای {item.get('Name')} ({item.get('ErpCode')}): {e}")
                    continue

            if len(items) < PRODUCT_SYNC_PAGE_SIZE:
                break
            page += 1

        # رزرو سفارش‌هایی که فاکتورشان در هلو ثبت شده و حالا موجودی کالایشان با سینکِ *بعد از* ثبت فاکتور به‌روز شده
        # (Few هلو خودش کسر را دارد) آزاد می‌شود؛ بدون سینک سالم هیچ رزروی آزاد نمی‌شود (محافظه‌کارانه)
        from products.stock import release_synced
        released_reservations = release_synced()
        if released_reservations:
            logger.info(f"رزرو موجودی: {released_reservations} رزرو فاکتورشده پس از سینک آزاد شد.")

        fetched_total = len(fetched_erp_codes)
        logger.info(
            f"واکشی پایان یافت: {fetched_total} کالای یکتا | ساخته‌شده={created_count} "
            f"به‌روزشده={updated_count} حذف‌شده(نام)={excluded_count} غیرفعال(هلو)={inactive_count} "
            f"خدماتی={service_count} خطا={error_count}"
        )

        # اطلاع‌رسانی «موجود شد» به کاربرهای منتظر؛ این اپ نمی‌داند و لازم نیست بداند چه کسی
        # به این رویداد گوش می‌دهد (نگاه کنید products.signals.product_back_in_stock)
        if back_in_stock_ids:
            from products.signals import product_back_in_stock
            for changed_product in Product.objects.filter(id__in=back_in_stock_ids):
                product_back_in_stock.send_robust(sender=Product, product=changed_product)

        # --- مرحله‌ی پاک‌سازی: حذف فیزیکی کالاهایی که دیگر در هلو نیستند یا غیرفعال شده‌اند
        # (فقط اگر واکشی کامل و مطمئن بود) ---
        # نکته: کالاهای excluded_count (فیلتر نام) و inactive_count (IsActive=False) عمداً وارد
        # fetched_erp_codes نشده‌اند، پس برای مقایسه با تعداد گزارش‌شده‌ی هلو باید هر دو به
        # fetched_total اضافه شوند؛ وگرنه این مقایسه همیشه باعث رد شدن مرحله‌ی پاک‌سازی واقعی می‌شد
        excluded_total = excluded_count + inactive_count + service_count
        if fetch_failed:
            logger.warning("مرحله‌ی پاک‌سازی رد شد: واکشی صفحه‌بندی‌شده کامل نشد.")
        elif reported_count is None:
            logger.warning("مرحله‌ی پاک‌سازی رد شد: تعداد کل کالاها از /Product/count قابل تشخیص نبود.")
        elif abs((fetched_total + excluded_total) - reported_count) > PRODUCT_SYNC_COUNT_TOLERANCE:
            logger.warning(
                f"مرحله‌ی پاک‌سازی رد شد: تعداد واکشی‌شده ({fetched_total} + {excluded_total} حذف‌شده/غیرفعال/خدماتی) با "
                f"گزارش هلو ({reported_count}) مطابقت ندارد."
            )
        else:
            from products.services import delete_products_safely

            existing_erp_codes = set(
                Product.objects.exclude(erp_code__isnull=True).values_list('erp_code', flat=True)
            )
            vanished = list(existing_erp_codes - fetched_erp_codes)
            removed = 0
            for i in range(0, len(vanished), 500):  # محدودیت پارامتر IN در MSSQL
                chunk = vanished[i:i + 500]
                deleted_products, _ = delete_products_safely(Product.objects.filter(erp_code__in=chunk))
                removed += deleted_products
            logger.info(f"مرحله‌ی پاک‌سازی: {removed} کالای غایب/غیرفعال از هلو حذف شد.")

        return (
            f"fetched={fetched_total} reported={reported_count} created={created_count} "
            f"updated={updated_count} excluded={excluded_count} services={service_count} errors={error_count}"
        )

    except Exception as e:
        logger.error(f"سینک محصولات هلو کاملاً ناموفق بود: {e}")
        backoff = (self.request.retries + 1) * 300  # ۵، ۱۰، ۱۵ دقیقه؛ تسک idempotent است
        raise self.retry(exc=e, countdown=backoff)

# max_retries=None یعنی این تسک هرگز برای همیشه شکست نمی‌خورد؛ چون خودِ سفارش و تراکنش
# پرداخت مستقل از هلو در دیتابیس سایت قطعی ثبت شده‌اند (نگاه کنید payments/views.py)، حتی
# قطعی چندروزه شبکه/هلو هم نباید باعث شود سفارشی برای همیشه به هلو نرسد؛ فقط بعد از
# HOLOO_SYNC_STALL_THRESHOLD به مدیر برای پیگیری دستی خبر داده می‌شود (تلاش ادامه دارد).
def _flag_needs_attention(order, message):
    """
    خطای دائمیِ ثبت فاکتور: تلاش دوباره چیزی را درست نمی‌کند. سفارش علامت می‌خورد، مدیر یک‌بار مطلع می‌شود و تسک و
    بازبینی دیگر سراغش نمی‌روند تا مدیر مشکل را اصلاح کند و با اکشن «ثبت مجدد در هلو» دوباره به صف بفرستد.
    """
    first_time = not order.holoo_needs_attention
    order.holoo_needs_attention = True
    order.holoo_last_error = (message or '')[:500]
    order.save(update_fields=['holoo_needs_attention', 'holoo_last_error', 'updated_at'])
    logger.error("ثبت فاکتور سفارش %s در هلو نیاز به بررسی دستی دارد: %s", order.id, message)
    if first_time:
        from notifications.service import notify_admin
        notify_admin('critical_alert', message=f"ثبت فاکتور سفارش #{order.id} در هلو رد شد و نیاز به بررسی دستی دارد: {message}"[:300])
    return f"Needs attention: {message}"


@shared_task(bind=True, max_retries=None)
def send_order_to_holoo(self, order_id):
    """
    این تسک سفارش را از دیتابیس می‌خواند، آن را به فرمت وب‌سرویس هلو تبدیل کرده
    و از طریق HolooClient به عنوان فاکتور (نه پیش‌فاکتور) ثبت می‌کند.
    این کار صرف‌نظر از روش پرداخت (چکی/نقدی/ویژه) و مستقل از نتیجه پرداخت آنلاین انجام می‌شود.
    """
    from orders.models import Order
    from .client import HolooClient # ایمپورت کلاینت هوشمند

    if _writes_disabled():
        return "Skipped (holoo writes disabled)"

    try:
        order = Order.objects.get(id=order_id)
    except Order.DoesNotExist:
        # سفارش حذف شده یا هنوز commit نشده؛ در این حالت تلاش مجدد فایده‌ای ندارد
        logger.error(f"سفارش {order_id} برای ارسال به هلو پیدا نشد.")
        return "Order not found."

    # --- تأیید دومرحله‌ای: فاکتور قطعی فقط پس از «تأیید سفارش» توسط مدیر (orders/approval.py) صادر می‌شود؛ ثبت سفارش
    # و پرداخت به‌تنهایی انبار هلو را تغییر نمی‌دهد. سفارش لغو/ردشده هم هرگز فاکتور نمی‌گیرد.
    if order.status in ('canceled', 'rejected_stock'):
        logger.info("سفارش %s در وضعیت %s است؛ فاکتور هلو ثبت نمی‌شود.", order.id, order.status)
        return "Order is not active."
    if not order.approved_at:
        logger.info("سفارش %s هنوز توسط مدیر تأیید نشده؛ فاکتور هلو ثبت نمی‌شود.", order.id)
        return "Awaiting admin approval."

    # --- پوکایوکه: جلوگیری از فاکتور تکراری در حسابداری ---
    # اگر تلاش قبلی در هلو موفق شده باشد ولی پاسخش به ما نرسیده باشد (timeout شبکه) یا این
    # تسک به هر دلیلی دوبار شلیک شود، بدون این چک هر retry یک فاکتور جدید در هلو می‌ساخت.
    if order.holoo_invoice_id:
        logger.info("سفارش %s از قبل در هلو ثبت شده (فاکتور %s)؛ ارسال دوباره انجام نشد.", order.id, order.holoo_invoice_id)
        return f"Already registered: {order.holoo_invoice_id}"

    # --- مشتری: فاکتور به ErpCode مشتری نیاز دارد (مهمان نداریم؛ خرید فقط با لاگین است). اگر کاربر هنوز در هلو ساخته نشده
    # (تسک همگام‌سازی نرسیده/شکست خورده)، همین‌جا ساخته می‌شود؛ خطای موقت ← retry، خطای دائمی ← نیاز به بررسی دستی.
    if order.user is None:
        return _flag_needs_attention(order, 'کاربر سفارش حذف شده؛ مشتریِ فاکتور مشخص نیست.')
    if not order.user.erp_code:
        customer = sync_customer(order.user, HolooClient())
        if not customer.get('success'):
            if customer.get('transient', True):
                logger.warning("ساخت مشتری سفارش %s در هلو ناموفق (موقت): %s", order.id, customer.get('message'))
                _alert_admin_if_holoo_sync_stalled(order)
                raise self.retry(countdown=min((self.request.retries ** 2) * 60, 3600))
            return _flag_needs_attention(order, f"ثبت مشتری در هلو رد شد: {customer.get('message')}")

    # ساختار آیتم‌های فاکتور
    sendable = []
    for item in order.items.select_related('product'):
        if item.product is None or not item.product.erp_code:
            # محصول از دیتابیس حذف شده (FK روی SET_NULL است) یا erp_code ندارد؛ بدون این چک
            # AttributeError می‌خورد و چون max_retries=None است تا ابد retry می‌شد
            logger.error("ردیف %s سفارش %s محصول/erp_code معتبر ندارد؛ از فاکتور هلو حذف شد.", item.id, order.id)
            continue
        sendable.append((item, item.product.erp_code))

    # فی ردیف‌ها؛ تخفیف سطح سفارش (کد تخفیف) متناسب روی فی پخش می‌شود (holoo/invoice.py::allocate_discount)
    items_payload = item_lines(order, sendable, f"ثبت از سایت - روش {order.payment_method}")

    if not items_payload:
        logger.critical("سفارش %s هیچ ردیف قابل‌ارسالی به هلو ندارد؛ نیاز به بررسی دستی.", order.id)
        return "No sendable items."

    # بدنه‌ی فاکتور: ردیف کرایه فقط برای ارسال با پیک (و سفارش‌های قدیمیِ بدون روش ارسال با کرایه‌ی > ۰) اضافه می‌شود،
    # هرگز برای پس‌کرایه‌ی پست؛ آدرس کامل تحویل در «توضیحات» فاکتور می‌رود (holoo/invoice.py).
    # کد کالای ردیف کرایه در تنظیمات سایت قابل تغییر است، نه هاردکد.
    from products.models import SiteSettings
    site = SiteSettings.cached()
    payload = build_invoice_payload(order, items_payload, site.shipping_erp_code, pos_sarfasl=site.holoo_pos_sarfasl)

    # جمع فاکتور هلو باید دقیقاً با مبلغ قابل‌پرداخت مشتری (که سند دریافت وجه با آن ثبت می‌شود) برابر باشد.
    # مغایرت مانع ارسال نمی‌شود (تلاش دوباره چیزی را درست نمی‌کند) ولی باید فوراً دیده شود.
    invoice_sum = payload_total(payload)
    if invoice_sum != order.total_price:
        logger.error(
            "مغایرت مبلغ فاکتور هلو برای سفارش %s: جمع ردیف‌ها %s ≠ مبلغ قابل‌پرداخت %s؛ نیاز به بررسی دستی.",
            order.id, invoice_sum, order.total_price,
        )

    # فرمول تلاش مجدد: (تعداد دفعات تلاش ^ 2) * ۶۰ ثانیه، با سقف ۱ ساعت (چون max_retries=None
    # است و ممکن است ده‌ها بار تلاش شود، بدون سقف فاصله‌ها به‌صورت نامعقولی طولانی می‌شدند)
    backoff_time = min((self.request.retries ** 2) * 60, 3600)

    # ارسال از طریق کلاینت (insert_invoice خطاهای شبکه‌ای/HTTP را خودش catch می‌کند و
    # به‌صورت دیکشنری success=False برمی‌گرداند؛ اینجا فقط برای خطاهای پیش‌بینی‌نشده احتیاط می‌کنیم)
    client = HolooClient()
    try:
        # قفل به‌ازای همین سفارش: اگر نسخه‌ی دیگری از این تسک (مثلاً از تسک بازبینی) هم‌زمان
        # در حال ارسال باشد، این یکی کنار می‌کشد. بدون این قفل، دو Worker می‌توانستند هر دو
        # قبل از ذخیره شدن InvoiceCode، فاکتور جداگانه‌ای در حسابداری بسازند.
        with task_lock(f'holoo:invoice:{order.id}', timeout=300) as acquired:
            if not acquired:
                logger.info("ارسال سفارش %s به هلو هم‌اکنون توسط اجرای دیگری در جریان است؛ این اجرا رد شد.", order.id)
                return "Skipped (locked)"
            result = client.insert_invoice(payload)
    except Exception as e:
        logger.warning(f"خطای سیستمی هنگام ارسال سفارش {order.id} به هلو: {e}. تلاش مجدد در {backoff_time} ثانیه دیگر...")
        _alert_admin_if_holoo_sync_stalled(order)
        raise self.retry(exc=e, countdown=backoff_time)

    if result.get('success'):
        order.holoo_invoice_id = result.get('InvoiceCode')
        order.holoo_invoice_erp_code = result.get('ErpCode')
        order.holoo_needs_attention = False
        order.holoo_last_error = ''
        updated_fields = ['holoo_invoice_id', 'holoo_invoice_erp_code', 'holoo_needs_attention', 'holoo_last_error', 'updated_at']
        # سفارش پرداخت‌شده با کارتخوان تسویه‌شده ثبت می‌شود (holoo/wire.py)؛ شماره‌ی سند حسابداریِ همان فاکتور، سند دریافت
        # آن است و دیگر سند دریافت جداگانه (که id آن در هلو یکتا نیست) لازم نیست
        if payload.get('Paid') and result.get('SanadCode') and not order.holoo_receipt_id:
            order.holoo_receipt_id = result['SanadCode']
            updated_fields.append('holoo_receipt_id')
        # رزرو موجودی تا سینک بعدیِ موجودی نگه داشته می‌شود (Few هلو شاید هنوز کسر نشده باشد)؛ بعد آزاد می‌شود
        from products.stock import mark_invoiced
        mark_invoiced(order.id)
        # اگر تا این لحظه کاربر پرداخت آنلاین را هم کامل کرده باشد (این تسک پس‌زمینه‌ست و ممکنه دیرتر از پرداخت اجرا شود)،
        # نباید وضعیت پیشرفته‌تر سفارش (مثلاً پردازش/ارسال) را عقب بیندازیم؛ فقط از حالت اولیه به ثبت‌شده منتقل می‌کنیم.
        # نکته: عمداً وضعیت را از دیتابیس تازه می‌خوانیم و فقط همان چند فیلد را می‌نویسیم، چون این
        # تسک ممکن است هم‌زمان با confirm_payment_in_holoo اجرا شود؛ با order.save() کامل، وضعیت
        # «processing» که آن تسک نوشته بود با نسخه‌ی کهنه‌ی داخل حافظه بازنویسی می‌شد.
        current_status = Order.objects.filter(pk=order.pk).values_list('status', flat=True).first()
        if current_status == 'pending':
            order.status = 'registered'
            updated_fields.append('status')
        order.save(update_fields=updated_fields)
        logger.info(f"سفارش {order.id} با موفقیت در هلو ثبت شد. کد فاکتور: {order.holoo_invoice_id}")

        # اگر کاربر زودتر از ثبت فاکتور پرداخت کرده باشد، تسک ثبت سند دریافت وجه ممکن است
        # همان موقع بی‌نتیجه برگشته باشد؛ حالا که فاکتور آماده است دوباره شلیکش می‌کنیم
        if order.is_paid and not order.holoo_receipt_id:
            confirm_payment_in_holoo.delay(order.id)

        return f"Success: {order.holoo_invoice_id}"

    # خطای ۲۸ هلو («کالاهای زیر فاقد موجودی»): تلاش دوباره چیزی را درست نمی‌کند (هلو داور موجودی است). سفارش به
    # rejected_stock می‌رود، رزرو آزاد و مدیر مطلع می‌شود؛ بازگشت وجه فقط با تصمیم دستی مدیر.
    if str(result.get('code') or '') == '28':
        from orders.stock_hooks import reject_for_stock
        reject_for_stock(order, result.get('message') or 'هلو: کالاها فاقد موجودی است (خطای ۲۸)')
        return "Rejected: stock (Holoo error 28)"

    # خطای دائمیِ داده (کد کالای نامعتبر، تاریخ/تسویه نامعتبر، ...): تلاش دوباره فایده ندارد؛ علامت و اطلاع به مدیر.
    # خطای موقت (قطعی شبکه/هلو، لاگین، HTTP 5xx، پاسخ ناقص) بدون سقف تعداد با فاصله‌ی افزایشی دوباره تلاش می‌شود.
    if not result.get('transient', True):
        return _flag_needs_attention(order, f"هلو فاکتور را رد کرد (کد {result.get('code')}): {result.get('message')}")

    logger.warning(f"خطا در ثبت سفارش {order.id} در هلو: {result.get('message')}. تلاش مجدد در {backoff_time} ثانیه دیگر...")
    _alert_admin_if_holoo_sync_stalled(order)
    raise self.retry(countdown=backoff_time)

# max_retries=None به همان دلیل send_order_to_holoo: پول از مشتری گرفته شده و در دیتابیس سایت
# قطعی ثبت است؛ هیچ قطعی شبکه/هلویی نباید باعث شود سند دریافت وجه برای همیشه ثبت نشود.
# (نسخه‌ی قبلی این تسک هیچ retry نداشت: اگر پرداخت زودتر از ثبت فاکتور انجام می‌شد — که در
# عمل همیشه اتفاق می‌افتد چون هر دو تسک پس‌زمینه‌اند — با "No Invoice" برمی‌گشت و سند
# دریافت وجه آن سفارش برای همیشه گم می‌شد.)
@shared_task(bind=True, max_retries=None)
def confirm_payment_in_holoo(self, order_id):
    """
    پس از پرداخت آنلاین موفق: سند دریافت وجه را برای فاکتورِ از قبل ثبت‌شده‌ی سفارش در هلو
    ثبت می‌کند و فقط پس از پاسخ موفق هلو، وضعیت سفارش را به «در حال آماده‌سازی انبار» می‌برد.
    """
    from orders.models import Order
    from .client import HolooClient

    if _writes_disabled():
        return "Skipped (holoo writes disabled)"

    backoff_time = min((self.request.retries ** 2) * 60, 3600)

    try:
        order = Order.objects.get(id=order_id)
    except Order.DoesNotExist:
        logger.error("سفارش %s برای ثبت سند دریافت وجه پیدا نشد.", order_id)
        return "Order not found."

    # --- پوکایوکه: جلوگیری از سند دریافت وجه تکراری ---
    if order.holoo_receipt_id:
        logger.info("سند دریافت وجه سفارش %s از قبل ثبت شده (%s).", order.id, order.holoo_receipt_id)
        return f"Already registered: {order.holoo_receipt_id}"

    if not order.is_paid:
        # پرداخت موفقی وجود ندارد؛ تلاش مجدد بی‌معناست
        logger.warning("سفارش %s تراکنش موفق ندارد؛ ثبت سند دریافت وجه انجام نشد.", order.id)
        return "Not paid."

    if not order.approved_at:
        # فاکتور تا «تأیید مدیر» صادر نمی‌شود؛ پس از ثبت فاکتور، send_order_to_holoo همین تسک را دوباره شلیک می‌کند
        logger.info("سفارش %s هنوز تأیید مدیر ندارد؛ ثبت سند دریافت وجه موکول به پس از ثبت فاکتور است.", order.id)
        return "Awaiting admin approval."

    if not order.holoo_invoice_id:
        # فاکتور هنوز در هلو ثبت نشده (تسک send_order_to_holoo هنوز تمام نشده یا در حال retry است).
        # این یک خطای دائمی نیست، پس منتظر می‌مانیم و دوباره تلاش می‌کنیم.
        logger.info("سفارش %s هنوز فاکتوری در هلو ندارد؛ ثبت سند دریافت وجه %s ثانیه دیگر دوباره تلاش می‌شود.", order.id, backoff_time)
        _alert_admin_if_holoo_sync_stalled(order, what='ثبت سند دریافت وجه')
        raise self.retry(countdown=backoff_time)

    logger.info("شروع ثبت سند دریافت وجه سفارش %s (فاکتور %s) در هلو...", order.id, order.holoo_invoice_id)
    client = HolooClient()
    try:
        # همان دلیل قفلِ ثبت فاکتور: جلوگیری از دو سند دریافت وجه برای یک سفارش
        with task_lock(f'holoo:receipt:{order.id}', timeout=300) as acquired:
            if not acquired:
                logger.info("ثبت سند دریافت وجه سفارش %s هم‌اکنون در جریان است؛ این اجرا رد شد.", order.id)
                return "Skipped (locked)"
            result = client.register_payment(order.holoo_invoice_id, float(order.total_price))
    except Exception as e:
        logger.warning("خطای سیستمی هنگام ثبت سند دریافت وجه سفارش %s: %s. تلاش مجدد در %s ثانیه.", order.id, e, backoff_time)
        _alert_admin_if_holoo_sync_stalled(order, what='ثبت سند دریافت وجه')
        raise self.retry(exc=e, countdown=backoff_time)

    if result.get('success'):
        order.holoo_receipt_id = result.get('ReceiptCode')
        order.status = 'processing'
        order.save(update_fields=['holoo_receipt_id', 'status', 'updated_at'])
        logger.info("سند دریافت وجه سفارش %s با موفقیت در هلو ثبت شد: %s", order.id, order.holoo_receipt_id)
        return f"Payment Registered: {order.holoo_receipt_id}"

    logger.warning("خطا در ثبت سند دریافت وجه سفارش %s: %s. تلاش مجدد در %s ثانیه.", order.id, result.get('message'), backoff_time)
    _alert_admin_if_holoo_sync_stalled(order, what='ثبت سند دریافت وجه')
    raise self.retry(countdown=backoff_time)


@shared_task
def reconcile_holoo_orders():
    """
    تور ایمنی (safety net) دوره‌ای: سفارش‌هایی که در چرخه‌ی عادی از قلم افتاده‌اند را دوباره
    به صف می‌فرستد. لازم است چون شلیک تسک از داخل ویو می‌تواند شکست بخورد (مثلاً Redis در آن
    لحظه پایین باشد) یا Worker وسط کار کشته شود و آن اجرا برای همیشه گم شود.

    idempotent است: هر دو تسک مقصد اگر کار از قبل انجام شده باشد بلافاصله برمی‌گردند.
    """
    from orders.models import Order

    cutoff = timezone.now() - HOLOO_RECONCILE_MIN_AGE

    missing_invoice = list(
        Order.objects.filter(holoo_invoice_id__isnull=True, approved_at__isnull=False, holoo_needs_attention=False,
                             created_at__lt=cutoff)
        .exclude(status__in=('canceled', 'rejected_stock'))
        .values_list('id', flat=True)
    )

    missing_receipt = list(
        Order.objects.filter(
            holoo_receipt_id__isnull=True,
            holoo_invoice_id__isnull=False,
            transactions__status='success',
            created_at__lt=cutoff,
        ).exclude(status__in=('canceled', 'rejected_stock')).distinct().values_list('id', flat=True)
    )

    for order_id in missing_invoice:
        send_order_to_holoo.delay(order_id)
    for order_id in missing_receipt:
        confirm_payment_in_holoo.delay(order_id)

    if missing_invoice or missing_receipt:
        logger.info("بازبینی هلو: %s فاکتور و %s سند دریافت وجه دوباره به صف رفت.", len(missing_invoice), len(missing_receipt))
    return f"invoices={len(missing_invoice)} receipts={len(missing_receipt)}"