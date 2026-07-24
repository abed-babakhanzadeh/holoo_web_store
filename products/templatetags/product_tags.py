from django import template

register = template.Library()

@register.simple_tag
def get_user_price(product, user):
    """
    این تگ قیمت کالا را با توجه به سطح کاربر برمی‌گرداند
    """
    return product.get_user_price(user)


@register.simple_tag
def get_discounted_price(product, user):
    """ قیمت نهایی محصول با احتساب سطح کاربر و تخفیف فعال (اگر وجود داشته باشد) """
    return product.get_discounted_price(user)
