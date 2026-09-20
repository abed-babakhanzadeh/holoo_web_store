import logging
from datetime import timedelta
from celery import shared_task
from django.apps import apps
from django.utils import timezone
from django.utils.text import slugify

from .client import HolooClient
from .locks import task_lock

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

def _holoo_address(user):
    """ آدرس مشتری در هلو = آدرس پیش‌فرض کاربر (استان، شهر، ناحیه، آدرس)؛ کاربر بدون آدرس ← رشته‌ی خالی """
    address = user.default_address
    return address.full_text if address else ''


# max_retries=10 یعنی تا 10 بار تلاش میکنه (طی چند روز!)
@shared_task(bind=True, max_retries=10)
def sync_user_to_holoo(self, user_id):
    from accounts.models import UserStatus 
    CustomUser = apps.get_model('accounts', 'CustomUser')
    
    try:
        user = CustomUser.objects.get(id=user_id)
    except CustomUser.DoesNotExist:
        return "User not found."

    client = HolooClient()
    
    # ---------------------------------------------------------
    # پوکایوکه ۱: تفکیک ساخت مشتری جدید از آپدیت مشتری قدیمی
    # ---------------------------------------------------------
    if user.erp_code:
        # کاربر قبلا در هلو بوده، پس فقط باید آپدیت شود (این متد باید در client ساخته شود)
        logger.info(f"شروع آپدیت کاربر {user.phone_number} در هلو...")
        result = client.update_person(
            erp_code=user.erp_code,
            first_name=user.first_name,
            last_name=user.last_name,
            address=_holoo_address(user),
            # سایر فیلدها...
        )
    else:
        # مشتری جدید است، باید ساخته شود
        logger.info(f"شروع ثبت مشتری جدید {user.phone_number} در هلو...")
        result = client.insert_person(
            first_name=user.first_name,
            last_name=user.last_name,
            phone_number=user.phone_number,
            national_code=user.national_code,
            address=_holoo_address(user),
        )

    # بررسی نتیجه
    if result.get('success'):
        if not user.erp_code:
            user.erp_code = result.get('erp_code')
        user.status = UserStatus.ACTIVE
        user.last_sync_error = None
        user.retry_count = 0
        user.save()
        return "Sync Success"
    else:
        error_msg = result.get('message', 'خطای نامشخص هلو')
        error_code = result.get('code') # فرض میکنیم کلاینت کد خطا را هم برمیگرداند
        
        user.last_sync_error = error_msg
        user.retry_count += 1
        user.save()
        
        # ---------------------------------------------------------
        # پوکایوکه ۲: توقف تلاش برای خطاهای دیتایی (مثل خطای ۲۳ هلو)
        # ---------------------------------------------------------
        if error_code in ['23', '10', '8']: # کدهای خطای تکراری بودن هلو
            logger.error(f"خطای دیتایی غیرقابل حل: {error_msg}. توقف تلاش.")
            # اینجا وضعیت کاربر را روی PENDING نگه میداریم تا خودش بیاید دیتا را اصلاح کند
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
    - در پایان، اگر کل کاتالوگ با موفقیت و بدون خطا واکشی شده باشد (بر اساس مقایسه با
      /Product/count)، محصولاتی که دیگر در فهرست هلو نیستند is_active=False می‌شوند
      (هرگز حذف فیزیکی نمی‌شوند). اگر واکشی ناقص بود، این مرحله رد می‌شود تا داده‌ای
      به‌اشتباه از دست نرود.
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
        created_count = updated_count = error_count = excluded_count = 0
        fetch_failed = False
        back_in_stock_ids = []
        page = 1

        while page <= PRODUCT_SYNC_MAX_PAGES:
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
                    if not erp_code:
                        logger.warning(f"کالای بدون ErpCode رد شد: {item.get('Name')}")
                        continue

                    name = item.get('Name') or erp_code
                    if any(pattern in name for pattern in PRODUCT_NAME_EXCLUDE_PATTERNS):
                        # کالاهای «مصرف‌کننده/000» اصلاً وارد سایت نمی‌شوند؛ چون erp_code‌شان به
                        # fetched_erp_codes اضافه نمی‌شود، اگر قبلاً روی سایت بودند مرحله‌ی
                        # پاک‌سازی پایین همین تابع خودکار is_active=False‌شان می‌کند
                        excluded_count += 1
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

                    # --- فیلدهای مالی/انبار ---
                    price = _safe_float(item.get('SellPrice'))
                    stock = _safe_float(item.get('Few'))
                    price_tiers = {f'price{i}': _safe_float(item.get(f'SellPrice{i}')) for i in range(2, 11)}
                    is_active = bool(item.get('IsActive', True))
                    product_code = item.get('Code')

                    product, created = Product.objects.get_or_create(
                        erp_code=erp_code,
                        defaults={
                            'name': name,
                            'slug': _unique_product_slug(name, erp_code),
                            'product_code': product_code,
                            'category': category_to_assign,  # فقط این‌جا، در لحظه‌ی ساخت، ست می‌شود
                            'price': price,
                            'stock': stock,
                            'is_active': is_active,
                            **price_tiers,
                        }
                    )

                    if created:
                        created_count += 1
                    else:
                        # category و slug عمداً دست‌نخورده می‌مانند (تصمیم ادمین/URL محصول حفظ می‌شود)
                        was_out_of_stock = product.stock <= 0
                        product.name = name
                        product.product_code = product_code
                        product.price = price
                        product.stock = stock
                        product.is_active = is_active
                        for field, value in price_tiers.items():
                            setattr(product, field, value)
                        product.save()
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

        fetched_total = len(fetched_erp_codes)
        logger.info(
            f"واکشی پایان یافت: {fetched_total} کالای یکتا | ساخته‌شده={created_count} "
            f"به‌روزشده={updated_count} حذف‌شده(نام)={excluded_count} خطا={error_count}"
        )

        # اطلاع‌رسانی «موجود شد» به کاربرهای منتظر؛ این اپ نمی‌داند و لازم نیست بداند چه کسی
        # به این رویداد گوش می‌دهد (نگاه کنید products.signals.product_back_in_stock)
        if back_in_stock_ids:
            from products.signals import product_back_in_stock
            for changed_product in Product.objects.filter(id__in=back_in_stock_ids):
                product_back_in_stock.send_robust(sender=Product, product=changed_product)

        # --- مرحله‌ی پاک‌سازی: مخفی‌کردن کالاهایی که دیگر در هلو نیستند (فقط اگر واکشی کامل و مطمئن بود) ---
        # نکته: کالاهای excluded_count عمداً وارد fetched_erp_codes نشده‌اند (فیلتر نام)، پس برای
        # مقایسه با تعداد گزارش‌شده‌ی هلو باید به fetched_total اضافه شوند؛ وگرنه این فیلتر همیشه
        # باعث رد شدن مرحله‌ی پاک‌سازی واقعی می‌شد (چون تعداد همیشه excluded_count تا کمتر می‌بود)
        if fetch_failed:
            logger.warning("مرحله‌ی پاک‌سازی رد شد: واکشی صفحه‌بندی‌شده کامل نشد.")
        elif reported_count is None:
            logger.warning("مرحله‌ی پاک‌سازی رد شد: تعداد کل کالاها از /Product/count قابل تشخیص نبود.")
        elif abs((fetched_total + excluded_count) - reported_count) > PRODUCT_SYNC_COUNT_TOLERANCE:
            logger.warning(
                f"مرحله‌ی پاک‌سازی رد شد: تعداد واکشی‌شده ({fetched_total} + {excluded_count} حذف‌شده) با گزارش هلو "
                f"({reported_count}) مطابقت ندارد."
            )
        else:
            existing_erp_codes = set(
                Product.objects.exclude(erp_code__isnull=True).values_list('erp_code', flat=True)
            )
            vanished = list(existing_erp_codes - fetched_erp_codes)
            hidden = 0
            for i in range(0, len(vanished), 500):  # محدودیت پارامتر IN در MSSQL
                chunk = vanished[i:i + 500]
                hidden += Product.objects.filter(erp_code__in=chunk, is_active=True).update(is_active=False)
            logger.info(f"مرحله‌ی پاک‌سازی: {hidden} کالای غایب از هلو مخفی شد.")

        return (
            f"fetched={fetched_total} reported={reported_count} created={created_count} "
            f"updated={updated_count} excluded={excluded_count} errors={error_count}"
        )

    except Exception as e:
        logger.error(f"سینک محصولات هلو کاملاً ناموفق بود: {e}")
        backoff = (self.request.retries + 1) * 300  # ۵، ۱۰، ۱۵ دقیقه؛ تسک idempotent است
        raise self.retry(exc=e, countdown=backoff)

# max_retries=None یعنی این تسک هرگز برای همیشه شکست نمی‌خورد؛ چون خودِ سفارش و تراکنش
# پرداخت مستقل از هلو در دیتابیس سایت قطعی ثبت شده‌اند (نگاه کنید payments/views.py)، حتی
# قطعی چندروزه شبکه/هلو هم نباید باعث شود سفارشی برای همیشه به هلو نرسد؛ فقط بعد از
# HOLOO_SYNC_STALL_THRESHOLD به مدیر برای پیگیری دستی خبر داده می‌شود (تلاش ادامه دارد).
@shared_task(bind=True, max_retries=None)
def send_order_to_holoo(self, order_id):
    """
    این تسک سفارش را از دیتابیس می‌خواند، آن را به فرمت وب‌سرویس هلو تبدیل کرده
    و از طریق HolooClient به عنوان فاکتور (نه پیش‌فاکتور) ثبت می‌کند.
    این کار صرف‌نظر از روش پرداخت (چکی/نقدی/ویژه) و مستقل از نتیجه پرداخت آنلاین انجام می‌شود.
    """
    from orders.models import Order
    from .client import HolooClient # ایمپورت کلاینت هوشمند

    try:
        order = Order.objects.get(id=order_id)
    except Order.DoesNotExist:
        # سفارش حذف شده یا هنوز commit نشده؛ در این حالت تلاش مجدد فایده‌ای ندارد
        logger.error(f"سفارش {order_id} برای ارسال به هلو پیدا نشد.")
        return "Order not found."

    # --- پوکایوکه: جلوگیری از فاکتور تکراری در حسابداری ---
    # اگر تلاش قبلی در هلو موفق شده باشد ولی پاسخش به ما نرسیده باشد (timeout شبکه) یا این
    # تسک به هر دلیلی دوبار شلیک شود، بدون این چک هر retry یک فاکتور جدید در هلو می‌ساخت.
    if order.holoo_invoice_id:
        logger.info("سفارش %s از قبل در هلو ثبت شده (فاکتور %s)؛ ارسال دوباره انجام نشد.", order.id, order.holoo_invoice_id)
        return f"Already registered: {order.holoo_invoice_id}"

    # ساختار آیتم‌های فاکتور
    items_payload = []
    for item in order.items.select_related('product'):
        if item.product is None or not item.product.erp_code:
            # محصول از دیتابیس حذف شده (FK روی SET_NULL است) یا erp_code ندارد؛ بدون این چک
            # AttributeError می‌خورد و چون max_retries=None است تا ابد retry می‌شد
            logger.error("ردیف %s سفارش %s محصول/erp_code معتبر ندارد؛ از فاکتور هلو حذف شد.", item.id, order.id)
            continue
        items_payload.append({
            "ErpCode": item.product.erp_code,
            "Amount": int(item.quantity),
            "Price": float(item.price),
            "Comment": f"ثبت از سایت - روش {order.payment_method}"
        })

    if not items_payload:
        logger.critical("سفارش %s هیچ ردیف قابل‌ارسالی به هلو ندارد؛ نیاز به بررسی دستی.", order.id)
        return "No sendable items."

    # اضافه کردن هزینه ارسال؛ کد کالای آن در تنظیمات سایت قابل تغییر است، نه هاردکد
    if order.shipping_cost > 0:
        from products.models import SiteSettings
        items_payload.append({
            "ErpCode": SiteSettings.cached().shipping_erp_code,
            "Amount": 1,
            "Price": float(order.shipping_cost),
            "Comment": "هزینه ارسال و بسته‌بندی پستی"
        })

    # دریافت کد مشتری (اگر هنوز سینک نشده بود، کد مهمان/پیش‌فرض بگذار)
    customer_erp = order.user.erp_code if order.user.erp_code else "GUEST_CODE"

    # بدنه نهایی
    payload = {
        "CustomerErpCode": customer_erp,
        "Date": order.created_at.strftime("%Y/%m/%d"),
        "Comment": f"سفارش آنلاین سایت کد #{order.id}",
        "Items": items_payload
    }

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
        updated_fields = ['holoo_invoice_id', 'updated_at']
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

    # هلو موقتاً/به هر دلیلی رد کرده؛ چون insert_invoice کد خطای قابل‌اعتمادی برای تفکیک
    # خطای دیتایی دائمی از خطای موقت برنمی‌گرداند، همچنان (بدون سقف تعداد) دوباره تلاش می‌کنیم
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
        Order.objects.filter(holoo_invoice_id__isnull=True, created_at__lt=cutoff)
        .exclude(status='canceled')
        .values_list('id', flat=True)
    )

    missing_receipt = list(
        Order.objects.filter(
            holoo_receipt_id__isnull=True,
            holoo_invoice_id__isnull=False,
            transactions__status='success',
            created_at__lt=cutoff,
        ).exclude(status='canceled').distinct().values_list('id', flat=True)
    )

    for order_id in missing_invoice:
        send_order_to_holoo.delay(order_id)
    for order_id in missing_receipt:
        confirm_payment_in_holoo.delay(order_id)

    if missing_invoice or missing_receipt:
        logger.info("بازبینی هلو: %s فاکتور و %s سند دریافت وجه دوباره به صف رفت.", len(missing_invoice), len(missing_receipt))
    return f"invoices={len(missing_invoice)} receipts={len(missing_receipt)}"