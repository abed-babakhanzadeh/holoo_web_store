from django import template

register = template.Library()

@register.simple_tag
def get_user_price(product, user):
    """
    این تگ قیمت کالا را با توجه به سطح کاربر برمی‌گرداند
    """
    return product.get_user_price(user)
