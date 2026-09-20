"""
اسنپ‌شات مقصد/گیرنده/ارسال برای سفارش.

هنگام ثبت سفارش، همه‌ی اطلاعاتِ آدرس و نتیجه‌ی محاسبه‌ی ارسال به‌صورت متن/عدد داخل خودِ Order کپی می‌شود
(نه ForeignKey)، تا تغییر بعدیِ آدرس کاربر یا تعرفه‌ی ناحیه فاکتورهای قدیمی را عوض نکند.
"""

from .shipping import ShippingQuote


def order_snapshot(address, quote: ShippingQuote):
    """
    دیکشنری فیلدهای Order را از (آدرسِ مالک‌سنجی‌شده، ShippingQuoteِ قابل‌ارسال) می‌سازد.
    برای quoteِ مسدود خطا می‌دهد: سفارشِ غیرقابل‌ارسال هرگز نباید ثبت شود.
    """
    if not quote.available:
        raise ValueError(f'ارسال ممکن نیست ({quote.reason}): {quote.message}')
    return {
        'first_name': address.receiver_first_name,
        'last_name': address.receiver_last_name,
        'phone': address.receiver_phone,
        'address': address.address,
        'postal_code': address.postal_code,
        'province': address.city.province.name,
        'city': address.city.name,
        'zone': address.zone.name if address.zone_id else '',
        'shipping_method': quote.method,
        'shipping_label': quote.label,
        'shipping_cost': quote.cost,
    }
