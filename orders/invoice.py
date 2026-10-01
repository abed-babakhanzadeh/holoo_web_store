"""
داده‌ی فاکتور سفارش (templates/orders/invoice.html): مشخصات خریدار از اسنپ‌شات سفارش، ردیف‌های اقلام با قیمت ثبت‌شده
و جمع‌ها. فاکتور عمداً ستون/فرمول مالیات و ارزش افزوده ندارد؛ مبلغ‌ها همان اعداد ثبت‌شده‌ی سفارش‌اند (تومان).
"""
from decimal import Decimal


def buyer_details(order):
    """ خریدار از اسنپ‌شات سفارش (نه پروفایل زنده)؛ فقط کد ملی از پروفایل می‌آید (در سفارش ذخیره نمی‌شود) """
    user = order.user
    name = f'{order.first_name} {order.last_name}'.strip()
    return {
        'name': name,
        'national_code': (getattr(user, 'national_code', '') or '').strip(),
        'address': order.full_address,
        'postal_code': (order.postal_code or '').strip(),
        'phone': (order.phone or '').strip(),
    }


WALLET_REF = 'کیف پول'                  # payments.checkout برای پرداخت فقط-کیف‌پول همین متن را در ref_id می‌نویسد (شماره‌ی بانکی نیست)


def tracking_reference(order, paid):
    """
    مقدار «پیگیری» سربرگ: شماره‌ی پیگیری بانکی (RefID) فقط وقتی تراکنش واقعاً از درگاه گذشته (amount > 0) و ref_id یک شماره‌ی
    بانکی باشد؛ پرداخت فقط-کیف‌پول ref_id متنیِ «کیف پول» دارد و نباید به‌عنوان پیگیری چاپ شود. در غیر این صورت کد رهگیری
    پستی سفارش، و اگر نبود شماره‌ی سفارش (مرجع) - هرگز خالی نمی‌ماند.
    """
    if paid and paid.amount and paid.ref_id and paid.ref_id.strip() != WALLET_REF:
        return paid.ref_id.strip()
    if order.tracking_code:
        return order.tracking_code
    return f'سفارش {order.pk}'


def payment_title(order, paid):
    """ «نقدی»، «چکی»، «ویژه»؛ پرداخت کامل با کیف پول «کیف پول» و پرداخت ترکیبی «نقدی و کیف پول» (بدون پرانتز سطح قیمت) """
    if paid and paid.wallet_amount:
        return 'کیف پول' if not paid.amount else f'{order.payment_method_title} و کیف پول'
    return order.payment_method_title


def build_order_invoice(order):
    """ ردیف‌ها و جمع‌های فاکتور؛ order باید items (با product/color) و transactions را prefetch کرده باشد """
    rows = []
    for index, item in enumerate(order.items.all(), start=1):
        product = item.product
        rows.append({
            'n': index,
            # خط اول: «کد کالا» (کد هلوی عددی کوتاه)؛ خط دوم: شناسه‌ی سیستمی بلند (ErpCode هلو)
            'code': (product.product_code or '') if product else '',
            'system_id': product.erp_code if product else '',
            'name': product.name if product else 'کالای حذف‌شده',
            'color': item.color.name if item.color else '',
            'unit': (product.unit if product else '') or '',
            'quantity': item.quantity,
            'unit_price': item.unit_original_price,
            'total': item.original_cost,
            'discount': item.line_discount,
            'after': item.get_cost(),
        })
    zero = Decimal('0')
    transactions = list(order.transactions.all())
    paid = next((t for t in transactions if t.status == 'success'), None)
    return {
        'rows': rows,
        'sum_quantity': sum((r['quantity'] for r in rows), 0),
        'sum_total': sum((r['total'] for r in rows), zero),
        'sum_discount': sum((r['discount'] for r in rows), zero),
        'sum_after': sum((r['after'] for r in rows), zero),
        'tracking': tracking_reference(order, paid),
        'payment_title': payment_title(order, paid),
    }
