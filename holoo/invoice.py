"""
ساخت بدنه‌ی فاکتور هلو از روی سفارش (تابع‌های ساده و قابل‌تست؛ بدون دسترسی به شبکه).

قواعد ردیف کرایه (بر اساس اسنپ‌شات روش ارسال روی خودِ سفارش):
  - shipping_method == 'courier' (پیک) و کرایه > ۰   ← یک ردیف کرایه با shipping_erp_code
  - shipping_method == 'post' (پس‌کرایه)            ← *هرگز* ردیفی نمی‌رود (حتی اگر مبلغی روی سفارش باشد؛
                                                        کرایه‌ی پست را گیرنده هنگام تحویل می‌پردازد)
  - shipping_method == '' (سفارش‌های قدیمی)         ← همان قاعده‌ی قبلی: فقط اگر کرایه > ۰ بود، با همان متن قبلی
  - هر مقدار ناشناخته                                ← ردیفی نمی‌رود (محتاطانه)

آدرس: بدنه‌ی فاکتور فیلد جدایی برای آدرس ندارد (مستند وب‌سرویس هلو در پروژه نیست)؛ آدرس کامل تحویل
(order.full_address: استان، شهر، ناحیه، آدرس) به‌همراه کدپستی و گیرنده در «توضیحات» فاکتور (Comment) درج می‌شود.
"""

COURIER = 'courier'
POST = 'post'

# متنی که پیش از فاز «کرایه بر اساس آدرس» روی ردیف کرایه می‌رفت؛ برای سفارش‌های قدیمی (روش ارسال خالی) دست‌نخورده می‌ماند
LEGACY_SHIPPING_COMMENT = 'هزینه ارسال و بسته‌بندی پستی'


def shipping_line(order, shipping_erp_code):
    """ ردیف کرایه‌ی فاکتور یا None (طبق قواعد بالا) """
    method = order.shipping_method or ''
    cost = order.shipping_cost
    if method == POST or method not in (COURIER, '') or not cost or cost <= 0:
        return None
    if method == COURIER:
        where = ' - '.join(part for part in (order.city, order.zone) if part)
        comment = f'کرایه پیک ({where})' if where else 'کرایه پیک'
    else:
        comment = LEGACY_SHIPPING_COMMENT
    return {
        'ErpCode': shipping_erp_code,
        'Amount': 1,
        'Price': float(cost),
        'Comment': comment,
    }


def invoice_comment(order):
    """ «توضیحات» فاکتور: شماره‌ی سفارش + آدرس کامل تحویل + کدپستی + گیرنده + روش ارسال """
    parts = [f'سفارش آنلاین سایت کد #{order.id}']
    address = order.full_address
    if address:
        parts.append(f'آدرس تحویل: {address}')
    if order.postal_code:
        parts.append(f'کد پستی: {order.postal_code}')
    receiver = f'{order.first_name} {order.last_name}'.strip()
    if receiver or order.phone:
        parts.append(f'گیرنده: {receiver} - {order.phone}'.rstrip(' -'))
    if order.shipping_method in (COURIER, POST) and order.shipping_label:
        parts.append(f'ارسال: {order.shipping_label}')
    return ' | '.join(parts)


def build_invoice_payload(order, items_payload, shipping_erp_code):
    """ بدنه‌ی نهایی فاکتور؛ items_payload ردیف‌های کالا هستند و ردیف کرایه (در صورت لزوم) به انتهایشان اضافه می‌شود """
    items = list(items_payload)
    line = shipping_line(order, shipping_erp_code)
    if line is not None:
        items.append(line)
    customer_erp = (order.user.erp_code if order.user else '') or 'GUEST_CODE'
    return {
        'CustomerErpCode': customer_erp,
        'Date': order.created_at.strftime('%Y/%m/%d'),
        'Comment': invoice_comment(order),
        'Items': items,
    }
