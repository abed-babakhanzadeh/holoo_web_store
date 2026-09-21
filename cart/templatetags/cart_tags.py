from django import template

from cart.models import CartItem

register = template.Library()

_REQUEST_CACHE_ATTR = '_cart_items_by_key'


def _cart_items_map(request, user):
    """
    یک‌بار در هر درخواست، کل اقلام سبد کاربر را می‌خواند و روی خودِ request کش می‌کند.

    چرا: این تگ در هر کارت محصول صدا زده می‌شود؛ روی صفحه‌ی فروشگاه با ۱۲ کارت یعنی ۱۲
    کوئری جداگانه. حالا برای هر تعداد کارت فقط یک کوئری زده می‌شود.
    """
    cached = getattr(request, _REQUEST_CACHE_ATTR, None)
    if cached is None:
        cached = {
            (item.product_id, item.color_id): item
            for item in CartItem.objects.filter(cart__user=user).select_related('product')
        }
        setattr(request, _REQUEST_CACHE_ATTR, cached)
    return cached


@register.simple_tag(takes_context=True)
def get_cart_item(context, product, user, color=None):
    """ بررسی می‌کند که آیا این کالا (با رنگ مشخص) در سبد خرید کاربر هست یا خیر """
    if not user.is_authenticated:
        return None

    request = context.get('request')
    if request is None:  # رندر خارج از چرخه‌ی درخواست (مثلاً ایمیل/تسک)
        return CartItem.objects.filter(cart__user=user, product=product, color=color).first()

    return _cart_items_map(request, user).get((product.id, color.id if color else None))

