from django import template
from cart.models import CartItem

register = template.Library()

@register.simple_tag
def get_cart_item(product, user, color=None):
    """ بررسی می‌کند که آیا این کالا (با رنگ مشخص) در سبد خرید کاربر هست یا خیر """
    if user.is_authenticated:
        return CartItem.objects.filter(cart__user=user, product=product, color=color).first()
    return None
