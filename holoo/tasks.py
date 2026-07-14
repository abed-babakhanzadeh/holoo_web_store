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
    
@shared_task(bind=True, max_retries=3)
def sync_products_from_holoo(self):
    """
    تسک پس‌زمینه برای دریافت محصولات و دسته‌بندی‌ها از هلو و ذخیره در سایت
    """
    Category = apps.get_model('products', 'Category')
    Product = apps.get_model('products', 'Product')
    
    client = HolooClient()
    logger.info("شروع دریافت لیست کالاها از هلو...")
    data = client.get_products()

    if not data or 'product' not in data:
        logger.warning("داده‌ای از هلو دریافت نشد یا فرمت اشتباه است.")
        return "No Data"

    products_list = data['product']
    synced_count = 0

    for item in products_list:
        try:
            # 1. مدیریت گروه‌های اصلی و فرعی (Category)
            main_group_erp = item.get('MainGroupErpCode')
            main_group_name = item.get('MainGroupName', 'بدون گروه اصلی')
            
            side_group_erp = item.get('SideGroupErpCode')
            side_group_name = item.get('SideGroupName', 'بدون گروه فرعی')

            # ساخت یا پیدا کردن گروه اصلی
            main_category, _ = Category.objects.get_or_create(
                erp_code=main_group_erp,
                defaults={
                    'name': main_group_name,
                    'slug': slugify(main_group_name, allow_unicode=True) + f"-{main_group_erp[-4:]}",
                    'parent': None
                }
            )

            # ساخت یا پیدا کردن گروه فرعی و اتصال آن به گروه اصلی
            side_category, _ = Category.objects.get_or_create(
                erp_code=side_group_erp,
                defaults={
                    'name': side_group_name,
                    'slug': slugify(side_group_name, allow_unicode=True) + f"-{side_group_erp[-4:]}",
                    'parent': main_category
                }
            )

            # 2. مدیریت محصولات (Product)
            product_erp = item.get('ErpCode')
            product_name = item.get('Name')
            product_code = item.get('Code')
            unit_name = item.get('UnitName', 'عدد')
            
            # تبدیل امن مقادیر عددی (قیمت 1 و موجودی)
            try:
                price = float(item.get('SellPrice', 0))
                stock = float(item.get('Few', 0))
            except (ValueError, TypeError):
                price = 0
                stock = 0

            # استخراج قیمت‌های 2 تا 10
            prices_dict = {}
            for i in range(2, 11):
                try:
                    p_val = float(item.get(f'SellPrice{i}', 0))
                except (ValueError, TypeError):
                    p_val = 0
                prices_dict[f'price{i}'] = p_val

            # ساخت اسلاگ یکتا برای محصول
            safe_slug = slugify(product_name, allow_unicode=True) + f"-{product_code}"

            # ترکیب دیکشنری دیفالت‌ها با قیمت‌های جدید
            defaults_data = {
                'name': product_name,
                'slug': safe_slug,
                'product_code': product_code,
                'category': side_category,
                'price': price,
                'stock': stock,
                'unit': unit_name,
            }
            defaults_data.update(prices_dict)

            # درج یا به‌روزرسانی هوشمند محصول
            Product.objects.update_or_create(
                erp_code=product_erp,
                defaults=defaults_data
            )
            
            synced_count += 1
            
        except Exception as e:
            logger.error(f"خطا در همگام‌سازی محصول {item.get('Name')}: {str(e)}")
            continue # رفتن به محصول بعدی در صورت بروز خطای موردی

    logger.info(f"همگام‌سازی پایان یافت. {synced_count} محصول بررسی/به‌روز شد.")
    return f"Synced {synced_count} products"

@shared_task
def send_order_to_holoo(order_id):
    """
    این تسک سفارش را از دیتابیس می‌خواند، آن را به فرمت وب‌سرویس هلو تبدیل کرده
    و از طریق HolooClient به عنوان پیش‌فاکتور ثبت می‌کند.
    """
    from orders.models import Order 
    from .client import HolooClient # ایمپورت کلاینت هوشمند
    
    try:
        order = Order.objects.get(id=order_id)
        
        # ساختار آیتم‌های پیش‌فاکتور
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
        result = client.insert_preinvoice(payload)
        
        if result.get('success'):
            order.holoo_preinvoice_id = result.get('PreInvoiceCode')
            # وضعیت را به "در حال پردازش" تغییر می‌دهیم
            order.status = 'registered'
            order.save()
            logger.info(f"سفارش {order.id} با موفقیت در هلو ثبت شد. کد پیش‌فاکتور: {order.holoo_preinvoice_id}")
            return f"Success: {order.holoo_preinvoice_id}"
        else:
            logger.error(f"خطا در ثبت سفارش {order.id} در هلو: {result.get('message')}")
            return "Failed"
            
    except Exception as e:
        logger.error(f"خطای سیستمی در تسک send_order_to_holoo: {str(e)}")
        return "Error"
    
@shared_task
def confirm_invoice_in_holoo(order_id):
    """ تسک پس‌زمینه برای قطعی کردن فاکتور در هلو پس از پرداخت موفق """
    from orders.models import Order
    from .client import HolooClient
    
    try:
        order = Order.objects.get(id=order_id)
        if not order.holoo_preinvoice_id:
            logger.error(f"سفارش {order.id} پیش‌فاکتوری در هلو ندارد که قطعی شود!")
            return "No PreInvoice"

        client = HolooClient()
        result = client.convert_to_invoice(order.holoo_preinvoice_id)
        
        if result.get('success'):
            logger.info(f"فاکتور قطعی برای سفارش {order.id} صادر شد: {result.get('InvoiceCode')}")
            # تغییر وضعیت به در حال پردازش (انبار)
            order.status = 'processing'
            order.save()
            return "Invoice Confirmed"
    except Exception as e:
        logger.error(f"خطا در تایید فاکتور سفارش {order_id}: {str(e)}")