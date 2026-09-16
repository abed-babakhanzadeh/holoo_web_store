from django import template

from wishlist.models import FavoriteProduct

register = template.Library()

_REQUEST_CACHE_ATTR = '_favorited_product_ids'


@register.simple_tag(takes_context=True)
def is_favorited(context, product, user):
    """
    بررسی می‌کند که آیا این محصول در علاقه‌مندی‌های کاربر هست یا خیر.

    شناسه‌ی همه‌ی علاقه‌مندی‌های کاربر یک‌بار در هر درخواست خوانده و روی request کش می‌شود؛
    قبلاً هر کارت محصول یک کوئری exists() جداگانه می‌زد (۱۲ کارت = ۱۲ کوئری).
    """
    if not user.is_authenticated:
        return False

    request = context.get('request')
    if request is None:  # رندر خارج از چرخه‌ی درخواست
        return FavoriteProduct.objects.filter(user=user, product=product).exists()

    ids = getattr(request, _REQUEST_CACHE_ATTR, None)
    if ids is None:
        ids = set(FavoriteProduct.objects.filter(user=user).values_list('product_id', flat=True))
        setattr(request, _REQUEST_CACHE_ATTR, ids)
    return product.id in ids
