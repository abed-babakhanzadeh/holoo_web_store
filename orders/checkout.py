"""
کمک‌های تسویه‌حساب: انتخاب آدرسِ مالک‌سنجی‌شده و محاسبه‌ی ارسال برای هر آدرس.

ورودی مرورگر فقط «شناسه‌ی آدرس» است؛ آدرس همیشه با فیلتر مالک (user=user) از دیتابیس خوانده می‌شود و کرایه/روش
ارسال را shipping_quote (orders/shipping.py) از روی داده‌ی دیتابیس می‌سازد، نه از هیچ فیلدی که مرورگر فرستاده.
"""

from accounts.models import Address

from .shipping import shipping_quote


def get_user_address(user, raw_id):
    """
    آدرسِ متعلق به *همین کاربر* با شناسه‌ی خام (رشته/عدد) یا None.
    شناسه‌ی نامعتبر، ناموجود یا مالِ کاربر دیگر همگی None می‌دهند (از هم قابل تشخیص نیستند).
    """
    try:
        pk = int(raw_id)
    except (TypeError, ValueError):
        return None
    return (Address.objects.select_related('city', 'city__province', 'zone')
            .filter(pk=pk, user=user).first())


def address_options(user, products, site_settings):
    """ همه‌ی آدرس‌های کاربر، هرکدام با ShippingQuote همین سبد (برای نمایش قابل‌ارسال/مسدود بودن هر کارت) """
    products = list(products)
    return [
        {'address': address, 'quote': shipping_quote(address, products, site_settings)}
        for address in user.addresses.select_related('city', 'city__province', 'zone')
    ]
