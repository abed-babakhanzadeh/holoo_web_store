"""
رجیستری «آخرین مقالات» برای صفحه اصلی.

مسئله‌ای که حل می‌کند: صفحه اصلی (اپ پایه‌ی products) باید چند مقاله‌ی تازه‌ی وبلاگ نشان
دهد، ولی طبق قاعده‌ی وابستگی پروژه یک اپ پایه نباید مستقیم مدل اپ‌های بالادست (اینجا:
blog) را import کند (نگاه کنید accounts/stats.py و reviews/purchases.py برای همین الگو).

راه‌حل: تک‌تأمین‌کننده، دقیقاً مثل reviews/purchases.py. اپ blog خودش را در
AppConfig.ready() ثبت می‌کند (blog/homepage.py). اگر اپ blog از پروژه حذف شود یا هنوز
ثبت نکرده باشد، صفحه اصلی فقط این بخش را خالی نشان می‌دهد و خطا نمی‌دهد.

نتیجه ۲۴ ساعت کش می‌شود (برخلاف products/home_cache.py که فقط id کش می‌کند، اینجا خودِ
رکوردها کش می‌شوند چون عنوان/خلاصه/تاریخ مقاله - برخلاف قیمت محصول - بین دو سینک هلو
عوض نمی‌شود). blog/homepage.py با سیگنال ذخیره/حذف Post این کش را فوراً باطل می‌کند.
"""

import logging

from django.core.cache import cache

logger = logging.getLogger(__name__)

_provider = None

CACHE_KEY = 'home:latest_posts'
CACHE_TTL = 24 * 60 * 60  # ۲۴ ساعت؛ محافظ در برابر از قلم‌افتادن سیگنال بطلان


def register_provider(func):
    """ تأمین‌کننده باید تابعی با امضای (limit) باشد که فهرست پست‌های منتشرشده را برمی‌گرداند """
    global _provider
    _provider = func
    return func


def latest_posts(limit=6):
    if _provider is None:
        return []
    posts = cache.get(CACHE_KEY)
    if posts is not None:
        return posts
    try:
        posts = _provider(limit)
    except Exception:
        logger.exception("دریافت آخرین مقالات وبلاگ برای صفحه اصلی ناموفق بود.")
        return []
    cache.set(CACHE_KEY, posts, CACHE_TTL)
    return posts


def clear_cache():
    cache.delete(CACHE_KEY)
