"""
ساخت بدنه‌ی فاکتور هلو از روی سفارش (تابع‌های ساده و قابل‌تست؛ بدون دسترسی به شبکه).

قواعد ردیف کرایه (بر اساس اسنپ‌شات روش ارسال روی خودِ سفارش):
  - shipping_method == 'courier' (پیک) و کرایه > ۰   ← یک ردیف کرایه با shipping_erp_code
  - shipping_method == 'post' (پس‌کرایه)            ← *هرگز* ردیفی نمی‌رود (حتی اگر مبلغی روی سفارش باشد؛
                                                        کرایه‌ی پست را گیرنده هنگام تحویل می‌پردازد)
  - shipping_method == '' (سفارش‌های قدیمی)         ← همان قاعده‌ی قبلی: فقط اگر کرایه > ۰ بود، با همان متن قبلی
  - هر مقدار ناشناخته                                ← ردیفی نمی‌رود (محتاطانه)

تخفیف سطح سفارش (کد تخفیف، Order.order_discount): بدنه‌ی فاکتور هلو ردیف «تخفیف» ندارد؛ مبلغ تخفیف
*متناسب با مبلغ هر ردیف* روی فی اقلام پخش می‌شود (allocate_discount) تا جمع اقلام فاکتور هلو دقیقاً با مبلغ کالاهای
پرداختی مشتری (کالاها − تخفیف) برابر شود. تخفیف‌های خودکار (Promotion) از قبل در فی ردیف‌ها (OrderItem.price) هست
و پخش نمی‌شود.

آدرس: بدنه‌ی فاکتور فیلد جدایی برای آدرس ندارد (مستند وب‌سرویس هلو در پروژه نیست)؛ آدرس کامل تحویل
(order.full_address: استان، شهر، ناحیه، آدرس) به‌همراه کدپستی و گیرنده در «توضیحات» فاکتور (Comment) درج می‌شود.
"""

from decimal import Decimal

COURIER = 'courier'
POST = 'post'

# متنی که پیش از فاز «کرایه بر اساس آدرس» روی ردیف کرایه می‌رفت؛ برای سفارش‌های قدیمی (روش ارسال خالی) دست‌نخورده می‌ماند
LEGACY_SHIPPING_COMMENT = 'هزینه ارسال و بسته‌بندی پستی'


def allocate_discount(rows, discount):
    """
    پخش متناسب یک تخفیفِ ریالی روی فی ردیف‌ها، با جمعِ دقیق (بدون حتی یک ریال اختلاف).

    rows: فهرست (فی هر واحد, تعداد) با اعداد صحیح؛ discount: مبلغ کل تخفیف (صحیح).
    خروجی: برای هر ردیفِ ورودی، فهرستی از (فی جدید, تعداد) — معمولاً یک عضو؛ فقط وقتی باقی‌مانده‌ی گرد کردن بر
    تعدادِ ردیف بخش‌پذیر نباشد، ردیف به دو بخش می‌شود که فی‌شان یک ریال فرق دارد (فیِ هلو صحیح است و نمی‌شود
    کسری از ریال را روی یک واحد گذاشت).

    الگوریتم (همه‌چیز عدد صحیح، بدون float):
      ۱. مبلغ هدف = جمع ردیف‌ها − تخفیف؛ فیِ هر ردیف متناسب کوچک و رو به پایین گرد می‌شود: فی × هدف ÷ جمع.
      ۲. جمعِ حاصل معمولاً کمی کمتر از هدف است؛ این «کسورِ گرد کردن» (Penny Rounding) به آخرین ردیف منتقل
         می‌شود (اگر ظرفیتش را نداشت، بقیه به ردیف‌های قبلی، از آخر به اول).
      ۳. هیچ فیِ جدیدی از فیِ اصلی بیشتر یا از صفر کمتر نمی‌شود؛ تخفیف بیشتر از جمع ردیف‌ها به جمع محدود می‌شود.
    """
    rows = [(int(price), int(quantity)) for price, quantity in rows]
    subtotal = sum(price * quantity for price, quantity in rows)
    discount = max(0, min(int(discount), subtotal))
    if not rows or discount == 0 or subtotal == 0:
        return [[(price, quantity)] for price, quantity in rows]

    target = subtotal - discount
    scaled = [price * target // subtotal for price, _ in rows]
    missing = target - sum(unit * quantity for unit, (_, quantity) in zip(scaled, rows))     # همیشه ≥ ۰

    extra = [0] * len(rows)
    for i in reversed(range(len(rows))):
        if missing <= 0:
            break
        price, quantity = rows[i]
        take = min(missing, quantity * (price - scaled[i]))
        extra[i] = take
        missing -= take

    result = []
    for (price, quantity), unit, take in zip(rows, scaled, extra):
        per_unit, remainder = divmod(take, quantity)
        unit += per_unit
        if remainder:
            result.append([(unit + 1, remainder), (unit, quantity - remainder)])
        else:
            result.append([(unit, quantity)])
    return result


def item_lines(order, sendable_rows, comment):
    """
    ردیف‌های کالای فاکتور. sendable_rows: فهرست (OrderItem, erp_code) قابل‌ارسال؛ comment: توضیح هر ردیف.
    اگر سفارش تخفیف سطح سفارش داشته باشد، فیِ ردیف‌ها با allocate_discount کم می‌شود و ردیفِ دوبخشی (فقط در
    صورت لزوم) دو ردیف با یک ErpCode می‌شود.
    """
    rows = [(item.price, item.quantity) for item, _ in sendable_rows]
    parts = allocate_discount(rows, order.order_discount or 0)
    lines = []
    for (item, erp_code), pieces in zip(sendable_rows, parts):
        for unit_price, quantity in pieces:
            lines.append({
                'ErpCode': erp_code,
                'Amount': int(quantity),
                'Price': float(unit_price),
                'Comment': comment,
            })
    return lines


def payload_total(payload):
    """ جمع مبلغِ ردیف‌های فاکتور (فی × تعداد)؛ برای سنجش با مبلغ قابل‌پرداخت سفارش """
    return sum(Decimal(str(row['Price'])) * Decimal(str(row['Amount'])) for row in payload['Items'])


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
    if order.order_discount and order.order_discount > 0:
        title = order.order_discount_label or 'تخفیف سفارش'
        parts.append(f'{title}: {int(order.order_discount)} (پخش‌شده روی فی اقلام)')
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
