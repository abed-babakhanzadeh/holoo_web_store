from django.core.cache import cache
from django.utils.functional import SimpleLazyObject

from .models import Category, SiteSettings

# داده‌های تقریباً ثابت (منوی دسته‌بندی‌ها و تنظیمات فوتر) این مدت کش می‌شوند و با هر
# ذخیره‌ی مربوطه در ادمین فوراً باطل می‌شوند (نگاه کنید products/signals.py)
NAV_CACHE_TTL = 15 * 60
CATEGORIES_CACHE_KEY = 'storefront:nav_categories'
BLOG_CATEGORIES_CACHE_KEY = 'storefront:nav_blog_categories'


def clear_storefront_cache():
    cache.delete_many([CATEGORIES_CACHE_KEY, BLOG_CATEGORIES_CACHE_KEY, SiteSettings.CACHE_KEY])


def _nav_categories():
    categories = cache.get(CATEGORIES_CACHE_KEY)
    if categories is None:
        categories = list(
            Category.objects.filter(is_active=True, parent__isnull=True)
            .prefetch_related('children').order_by('name')
        )
        cache.set(CATEGORIES_CACHE_KEY, categories, NAV_CACHE_TTL)
    return categories


def _nav_blog_categories():
    blog_categories = cache.get(BLOG_CATEGORIES_CACHE_KEY)
    if blog_categories is None:
        from blog.models import BlogCategory
        blog_categories = list(BlogCategory.objects.filter(is_active=True).order_by('name')[:6])
        cache.set(BLOG_CATEGORIES_CACHE_KEY, blog_categories, NAV_CACHE_TTL)
    return blog_categories


def _site_settings():
    return SiteSettings.cached()


def storefront(request):
    """
    داده‌های سراسری قالب (منوی دسته‌بندی‌ها، سبد خرید، شمارنده‌ها).

    دو بهینه‌سازی مهم نسبت به نسخه‌ی قبلی:

    ۱. همه‌ی مقادیر SimpleLazyObject هستند، یعنی فقط وقتی کوئری می‌خورند که قالبِ همان پاسخ
       واقعاً به آن‌ها دست بزند. این ویو روی *هر* درخواست اجرا می‌شود — از جمله ده‌ها درخواست
       کوچک htmx در یک صفحه (هر کارت محصول یک cart/status/ می‌زند) که هیچ‌کدام هدر و فوتر
       را رندر نمی‌کنند. قبلاً هر کدام از آن درخواست‌ها ۵-۶ کوئری بی‌استفاده می‌زد.

    ۲. داده‌های تقریباً ثابت کش می‌شوند و با ذخیره در ادمین باطل می‌شوند.
    """
    def _nav_cart():
        if not request.user.is_authenticated:
            return None
        from cart.models import Cart
        return (
            Cart.objects.filter(user=request.user)
            .select_related('user')  # get_cost به cart.user نیاز دارد
            .prefetch_related('items__product__discounts', 'items__color')
            .first()
        )

    def _favorite_count():
        if not request.user.is_authenticated:
            return 0
        from wishlist.models import FavoriteProduct
        return FavoriteProduct.objects.filter(user=request.user).count()

    return {
        'nav_categories': SimpleLazyObject(_nav_categories),
        'nav_blog_categories': SimpleLazyObject(_nav_blog_categories),
        'nav_cart': SimpleLazyObject(_nav_cart),
        'compare_count': len(request.session.get('compare_ids', [])),
        'favorite_count': SimpleLazyObject(_favorite_count),
        'site_settings': SimpleLazyObject(_site_settings),
    }
