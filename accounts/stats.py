"""
رجیستری آمار کاربر.

مسئله‌ای که حل می‌کند: پیشخوان پنل کاربری به داده‌ی چهار اپ دیگر نیاز دارد (سفارش، تراکنش،
علاقه‌مندی، بازدید اخیر). قبلاً accounts مستقیم مدل‌های هر چهار اپ را import می‌کرد، یعنی
پایین‌ترین لایه‌ی پروژه (اپ کاربر، که همه به آن وابسته‌اند) به بالاترین لایه‌ها وابسته بود.

راه‌حل: وارونه‌کردن جهت وابستگی. هر اپ خودش آمارش را در این رجیستری ثبت می‌کند و accounts
فقط نام‌ها را می‌خواند. حالا جهت وابستگی در کل پروژه یکدست است: همه -> accounts.

افزودن یک آمار جدید به پیشخوان = یک فایل stats.py در همان اپ + یک خط در ready() آن اپ.
حذف یک اپ از پروژه هم پیشخوان را نمی‌شکند (مقدار پیش‌فرض برگردانده می‌شود).
"""

import logging

logger = logging.getLogger(__name__)

_providers = {}


def register(name):
    """
    دکوریتور ثبت یک تأمین‌کننده‌ی آمار:

        @register('favorites_count')
        def favorites_count(user):
            return FavoriteProduct.objects.filter(user=user).count()
    """
    def decorator(func):
        _providers[name] = func
        return func
    return decorator


def get(name, user, default=None):
    """
    مقدار یک آمار. اگر اپ تأمین‌کننده نصب نباشد یا خطا بدهد، default برمی‌گردد؛
    خرابی یک آمار نباید کل صفحه‌ی پیشخوان را از کار بیندازد.
    """
    provider = _providers.get(name)
    if provider is None:
        return default
    try:
        return provider(user)
    except Exception:
        logger.exception("محاسبه‌ی آمار «%s» برای کاربر %s ناموفق بود.", name, getattr(user, 'id', None))
        return default


def collect(user, defaults):
    """ چند آمار را یک‌جا می‌خواند؛ defaults یک دیکشنری «نام -> مقدار پیش‌فرض» است """
    return {name: get(name, user, default) for name, default in defaults.items()}
