from django import template
from wishlist.models import FavoriteProduct

register = template.Library()

@register.simple_tag
def is_favorited(product, user):
    """ بررسی می‌کند که آیا این محصول در علاقه‌مندی‌های کاربر هست یا خیر """
    if user.is_authenticated:
        return FavoriteProduct.objects.filter(user=user, product=product).exists()
    return False
