"""
محدودیت حدس‌زدن کد تخفیف (Brute-force guard).

هر تلاشِ ناموفقِ «حدس کد» (کد ناموجود، غیرفعال، منقضی، یا تعریف‌نشده برای کاربر) برای هم کاربر و هم IP شمرده می‌شود؛
با رسیدن هرکدام به سقف (DiscountPolicy.coupon_max_invalid_attempts در هر coupon_attempt_window_minutes، پیش‌فرض ۱۰ در
ساعت) وارد کردن کد تا پایان بازه مسدود می‌شود. خطاهای مربوط به سبد (حداقل مبلغ، سقف مصرف، ...) شمرده نمی‌شوند؛
چون برای وارد کردنشان مهاجم باید کد درست را از قبل بداند.

شمارنده‌ها در کش مشترک (Redis) با پنجره‌ی ثابت نگه‌داری می‌شوند. کلید شامل نام دیتابیس است تا تست‌ها/سرور توسعه که Redis
مشترک دارند هم را آلوده نکنند. اگر Redis در دسترس نباشد محدودکننده «باز» می‌ماند (خطا لاگ می‌شود) تا خرید مشتری‌ها قطع نشود.
"""

import logging
import time

from django.core.cache import cache
from django.db import connection

from .models import DiscountPolicy

logger = logging.getLogger(__name__)


def _keys(user, ip):
    db = connection.settings_dict['NAME']
    keys = []
    if user is not None and getattr(user, 'pk', None):
        keys.append(f'coupon:fail:{db}:user:{user.pk}')
    if ip:
        keys.append(f'coupon:fail:{db}:ip:{ip}')
    return keys


def _limits():
    policy = DiscountPolicy.load()
    return policy.coupon_max_invalid_attempts, policy.coupon_attempt_window_minutes * 60


def check(user, ip):
    """ (مجاز؟، ثانیه‌ی باقی‌مانده تا پایان مسدودی) """
    cap, window = _limits()
    retry_after = 0
    try:
        for key in _keys(user, ip):
            state = cache.get(key)
            if state and state['count'] >= cap:
                retry_after = max(retry_after, int(state['start'] + window - time.time()) + 1)
    except Exception:                                    # noqa: BLE001
        logger.warning('بررسی محدودیت تلاش کد تخفیف ناموفق بود (محدودکننده باز ماند).', exc_info=True)
        return True, 0
    return retry_after <= 0, max(retry_after, 0)


def record_failure(user, ip):
    """ یک تلاش ناموفق برای کاربر و IP ثبت می‌کند """
    _, window = _limits()
    now = time.time()
    try:
        for key in _keys(user, ip):
            state = cache.get(key)
            if not state or state['start'] + window <= now:
                state = {'count': 0, 'start': now}
            state['count'] += 1
            cache.set(key, state, timeout=max(int(state['start'] + window - now), 1))
    except Exception:                                    # noqa: BLE001
        logger.warning('ثبت تلاش ناموفق کد تخفیف در کش ناموفق بود.', exc_info=True)


def reset(user=None, ip=None):
    """ پاک‌کردن شمارنده‌ها (ابزار ادمین/تست) """
    try:
        cache.delete_many(_keys(user, ip))
    except Exception:                                    # noqa: BLE001
        logger.warning('پاک‌کردن شمارنده‌ی تلاش کد تخفیف ناموفق بود.', exc_info=True)
