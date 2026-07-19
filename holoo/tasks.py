import logging
from celery import shared_task
from django.apps import apps
from .client import HolooClient
from django.utils.text import slugify
import requests
from django.conf import settings

logger = logging.getLogger(__name__)

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
            address=user.address,
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
            address=user.address
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
    Category = apps.get_model('products', 'Category')
    Product = apps.get_model('products', 'Product')
    client = HolooClient()

    try:
        reported_count = client.get_product_count()
        logger.info(f"هلو گزارش می‌دهد مجموعاً {reported_count} کالا دارد.")

        fetched_erp_codes = set()
        created_count = updated_count = error_count = 0
        fetch_failed = False
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
                    name = item.get('Name') or erp_code

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
                        product.name = name
                        product.product_code = product_code
                        product.price = price
                        product.stock = stock
                        product.is_active = is_active
                        for field, value in price_tiers.items():
                            setattr(product, field, value)
                        product.save()
                        updated_count += 1

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
            f"به‌روزشده={updated_count} خطا={error_count}"
        )

        # --- مرحله‌ی پاک‌سازی: مخفی‌کردن کالاهایی که دیگر در هلو نیستند (فقط اگر واکشی کامل و مطمئن بود) ---
        if fetch_failed:
            logger.warning("مرحله‌ی پاک‌سازی رد شد: واکشی صفحه‌بندی‌شده کامل نشد.")
        elif reported_count is None:
            logger.warning("مرحله‌ی پاک‌سازی رد شد: تعداد کل کالاها از /Product/count قابل تشخیص نبود.")
        elif abs(fetched_total - reported_count) > PRODUCT_SYNC_COUNT_TOLERANCE:
            logger.warning(
                f"مرحله‌ی پاک‌سازی رد شد: تعداد واکشی‌شده ({fetched_total}) با گزارش هلو "
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
            f"updated={updated_count} errors={error_count}"
        )

    except Exception as e:
        logger.error(f"سینک محصولات هلو کاملاً ناموفق بود: {e}")
        backoff = (self.request.retries + 1) * 300  # ۵، ۱۰، ۱۵ دقیقه؛ تسک idempotent است
        raise self.retry(exc=e, countdown=backoff)

@shared_task
def send_order_to_holoo(order_id):
    """
    این تسک سفارش را از دیتابیس می‌خواند، آن را به فرمت وب‌سرویس هلو تبدیل کرده
    و از طریق HolooClient به عنوان فاکتور (نه پیش‌فاکتور) ثبت می‌کند.
    این کار صرف‌نظر از روش پرداخت (نقدی/چکی/اقساطی) و مستقل از نتیجه پرداخت آنلاین انجام می‌شود.
    """
    from orders.models import Order
    from .client import HolooClient # ایمپورت کلاینت هوشمند

    try:
        order = Order.objects.get(id=order_id)

        # ساختار آیتم‌های فاکتور
        items_payload = []
        for item in order.items.all():
            items_payload.append({
                "ErpCode": item.product.erp_code,
                "Amount": int(item.quantity),
                "Price": float(item.price), 
                "Comment": f"ثبت از سایت - روش {order.payment_method}"
            })

        # اضافه کردن هزینه ارسال
        if order.shipping_cost > 0:
            items_payload.append({
                "ErpCode": "999999", 
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

        # ارسال از طریق کلاینت
        client = HolooClient()
        result = client.insert_invoice(payload)

        if result.get('success'):
            order.holoo_invoice_id = result.get('InvoiceCode')
            # اگر تا این لحظه کاربر پرداخت آنلاین را هم کامل کرده باشد (این تسک پس‌زمینه‌ست و ممکنه دیرتر از پرداخت اجرا شود)،
            # نباید وضعیت پیشرفته‌تر سفارش (مثلاً پردازش/ارسال) را عقب بیندازیم؛ فقط از حالت اولیه به ثبت‌شده منتقل می‌کنیم
            if order.status == 'pending':
                order.status = 'registered'
            order.save()
            logger.info(f"سفارش {order.id} با موفقیت در هلو ثبت شد. کد فاکتور: {order.holoo_invoice_id}")
            return f"Success: {order.holoo_invoice_id}"
        else:
            logger.error(f"خطا در ثبت سفارش {order.id} در هلو: {result.get('message')}")
            return "Failed"

    except Exception as e:
        logger.error(f"خطای سیستمی در تسک send_order_to_holoo: {str(e)}")
        return "Error"

@shared_task
def confirm_payment_in_holoo(order_id):
    """
    تسک پس‌زمینه‌ای که پس از پرداخت آنلاین موفق اجرا می‌شود: سند دریافت وجه را برای
    فاکتور از قبل ثبت‌شده‌ی سفارش در هلو ثبت می‌کند و فقط پس از پاسخ هلو (موفق)
    وضعیت سفارش را به «در حال آماده‌سازی انبار» تغییر می‌دهد.
    """
    from orders.models import Order
    from .client import HolooClient

    try:
        order = Order.objects.get(id=order_id)
        if not order.holoo_invoice_id:
            logger.error(f"سفارش {order.id} فاکتوری در هلو ندارد که سند دریافت وجه برایش ثبت شود!")
            return "No Invoice"

        logger.info(f"شروع ثبت سند دریافت وجه سفارش {order.id} (فاکتور {order.holoo_invoice_id}) در هلو...")
        client = HolooClient()
        result = client.register_payment(order.holoo_invoice_id, float(order.total_price))

        if result.get('success'):
            logger.info(f"سند دریافت وجه سفارش {order.id} با موفقیت در هلو ثبت شد: {result.get('ReceiptCode')}")
            order.status = 'processing'
            order.save()
            return f"Payment Registered: {result.get('ReceiptCode')}"
        else:
            logger.error(f"خطا در ثبت سند دریافت وجه سفارش {order.id} در هلو: {result.get('message')}")
            return "Failed"

    except Exception as e:
        logger.error(f"خطای سیستمی در تسک confirm_payment_in_holoo: {str(e)}")
        return "Error"