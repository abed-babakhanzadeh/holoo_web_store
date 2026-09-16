"""آمار بازدیدهای اخیر کاربر برای پیشخوان پنل کاربری."""

from accounts.stats import register

from .models import RecentlyViewed


@register('recently_viewed_count')
def recently_viewed_count(user):
    return RecentlyViewed.objects.filter(user=user).count()
