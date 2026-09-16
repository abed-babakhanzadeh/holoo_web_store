"""آمار علاقه‌مندی‌های کاربر برای پیشخوان پنل کاربری."""

from accounts.stats import register

from .models import FavoriteProduct


@register('favorites_count')
def favorites_count(user):
    return FavoriteProduct.objects.filter(user=user).count()
