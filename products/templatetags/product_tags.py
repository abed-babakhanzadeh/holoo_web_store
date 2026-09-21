from django import template

from products.pricing import base_price, final_price, price_breakdown

register = template.Library()


@register.simple_tag
def get_user_price(product, user):
    """
    قیمت پایه‌ی کالا برای این کاربر، *بدون* اعمال تخفیف.
    فقط برای نمایش قیمت خط‌خورده (del) کنار قیمت تخفیف‌خورده استفاده شود؛
    برای مبلغی که واقعاً از کاربر گرفته می‌شود از get_final_price استفاده کنید.
    """
    return base_price(product, user)


@register.simple_tag
def get_final_price(product, user):
    """
    مبلغی که واقعاً از کاربر گرفته می‌شود (سطح قیمت + تخفیف فعال).
    دقیقاً همان تابعی که سبد خرید و فاکتور هم از آن استفاده می‌کنند، تا عدد نمایشی
    هیچ‌وقت با عدد پرداختی اختلاف نداشته باشد.
    """
    return final_price(product, user)


@register.simple_tag
def get_discounted_price(product, user):
    """ هم‌معنی get_final_price؛ برای سازگاری با قالب‌های موجود نگه داشته شده """
    return final_price(product, user)


@register.simple_tag
def get_secondary_price(product, user):
    """ برای مشتری چکی/نقدی، قیمتِ نوع دیگر را برای نمایش کوچک‌تر در کنار قیمت اصلی برمی‌گرداند """
    return product.get_secondary_price(user)


@register.simple_tag
def price_info(product, user):
    """
    ریز قیمت (PriceBreakdown) برای نمایش: base، final، has_discount، percent، ends_at، badge_label.
    همان تابعی که سبد و فاکتور از آن استفاده می‌کنند، تا نشان تخفیف و قیمت با مبلغ پرداختی هیچ‌وقت اختلاف نداشته باشند.
    """
    return price_breakdown(product, user)
