"""
کش لیست‌های صفحه اصلی (جدیدترین/پرفروش‌ترین/پربازدیدترین محصولات، دسته‌های پرفروش،
برندهای محبوب). فقط شناسه‌ها (pk) کش می‌شوند، نه خودِ رکوردها؛ چون قیمت/موجودی
(سینک هلو) و تخفیف (پنجره‌ی زمانی دقیق) باید همیشه لحظه‌ای خوانده شوند - کد صدازننده بعد
از خواندن شناسه‌ها از این کش، رکوردهای واقعی را دوباره (ارزان، چون روی pk ایندکس‌شده)
می‌خواند. آخرین مقالات وبلاگ استثناست و در products/blog_posts.py با همین منطق ولی
مستقیماً به‌صورت رکورد کامل کش می‌شود، چون داده‌ی آن (عنوان/تاریخ/بازدید) قیمت زنده ندارد.

استراتژی بطلان: رویدادمحور (ذخیره/حذف محصول یا برند - products/signals.py؛ ثبت/تغییر
سفارش - orders/home_cache_hooks.py) + این TTL به‌عنوان محافظ، برای وقتی سیگنالی از قلم
بیفتد. «پربازدیدترین» عمداً از این بطلان رویدادمحور کنار گذاشته شده: رویداد پایه‌اش
(بازدید هر صفحه‌ی محصول) در هر بازدید شلیک می‌شود، پس بطلان رویدادمحور عملاً کش را
بی‌اثر می‌کرد؛ فقط TTL همین‌جا آن را تازه نگه می‌دارد.
"""

from django.core.cache import cache

HOME_CACHE_TTL = 24 * 60 * 60  # ۲۴ ساعت

NEWEST_IDS = 'home:newest_ids'
BEST_SELLING_IDS = 'home:best_selling_ids'
TOP_CATEGORY_IDS = 'home:top_category_ids'
POPULAR_BRAND_IDS = 'home:popular_brand_ids'
MOST_VIEWED_IDS = 'home:most_viewed_ids'  # عمداً در CATALOG_DEPENDENT_KEYS نیست؛ نگاه کنید بالا
STORIES = 'home:stories'  # برخلاف بقیه، رکورد کامل (نه فقط id) کش می‌شود - نگاه کنید products/views.py::_stories_data

CATALOG_DEPENDENT_KEYS = (NEWEST_IDS, BEST_SELLING_IDS, TOP_CATEGORY_IDS, POPULAR_BRAND_IDS)


def get_ids(key, compute_fn):
    """
    اگر کش نبود، compute_fn (بدون آرگومان) صدا زده و نتیجه برای HOME_CACHE_TTL کش
    می‌شود. علی‌رغم اسمش، برای هر مقدار قابل pickle (نه فقط لیست id) قابل استفاده
    است - همین تابع برای STORIES (لیست دیکشنری) هم به کار می‌رود.
    """
    value = cache.get(key)
    if value is None:
        value = compute_fn()
        cache.set(key, value, HOME_CACHE_TTL)
    return value


def clear_catalog_dependent_cache():
    cache.delete_many(CATALOG_DEPENDENT_KEYS)


def clear_stories_cache():
    cache.delete(STORIES)
