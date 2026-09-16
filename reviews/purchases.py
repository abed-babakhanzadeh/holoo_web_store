"""
تشخیص «خریدار تاییدشده» بدون وابستگی reviews به orders.

قبلاً reviews/models.py مستقیم orders.models.OrderItem را import می‌کرد. مثل رجیستری آمار
پیشخوان (accounts/stats.py)، اینجا هم جهت وابستگی وارونه شده: اپ orders خودش را به‌عنوان
تأمین‌کننده معرفی می‌کند و reviews فقط نتیجه را می‌گیرد.

اگر تأمین‌کننده‌ای ثبت نشده باشد (مثلاً اپ orders از پروژه حذف شود)، ثبت نظر همچنان کار
می‌کند و فقط برچسب «خریدار تاییدشده» زده نمی‌شود.
"""

import logging

logger = logging.getLogger(__name__)

_provider = None


def register_provider(func):
    """
    تأمین‌کننده باید تابعی با امضای (user, product) باشد که
    (خرید_کرده: bool, رنگ_خریداری‌شده: ProductColor | None) برگرداند.
    """
    global _provider
    _provider = func
    return func


def purchase_info(user, product):
    if _provider is None:
        return False, None
    try:
        return _provider(user, product)
    except Exception:
        logger.exception("بررسی سابقه‌ی خرید کاربر %s برای محصول %s ناموفق بود.",
                         getattr(user, 'id', None), getattr(product, 'id', None))
        return False, None
