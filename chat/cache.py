"""
لایه‌ی Redis برای چت: فقط شتاب‌دهنده و محدودکننده، هرگز منبع حقیقت. قطع Redis چت را متوقف نمی‌کند.

هر عملیات کش داخل try/except است و یک «مدارشکن» درون‌فرایندی دارد: بعد از اولین خطا ۳۰ ثانیه سراغ Redis نمی‌رود (بدون معطلی روی
timeout هر درخواست) و فراخوان‌کننده مقدار None می‌گیرد (= «کش در دسترس نیست»)؛ او باید با دیتابیس تصمیم بگیرد. لاگ خطا حداکثر
دقیقه‌ای یک بار است.
"""
import logging
import time

from django.core.cache import cache

logger = logging.getLogger(__name__)

PREFIX = 'chat:'
BREAKER_SECONDS = 30
_state = {'until': 0.0, 'logged': 0.0}


def _trip(error):
    now = time.monotonic()
    _state['until'] = now + BREAKER_SECONDS
    if now - _state['logged'] > 60:
        _state['logged'] = now
        logger.warning('Redis برای چت در دسترس نیست (%s)؛ چت با دیتابیس ادامه می‌دهد.', type(error).__name__)


def reset_breaker():
    """ برای تست """
    _state['until'] = 0.0


def available():
    return time.monotonic() >= _state['until']


def _call(func):
    """ نتیجه‌ی func() یا None اگر Redis قطع است (یا مدارشکن باز است) """
    if not available():
        return None
    try:
        return func()
    except Exception as error:  # noqa: BLE001 - خرابی کش نباید به مشتری برسد
        _trip(error)
        return None


def incr_window(key, window_seconds):
    """ شمارنده‌ی پنجره‌ای (الگوی accounts/throttle: cache.add فقط برای ساخت و TTL ثابت). ← عدد جدید یا None (قطع) """
    full = PREFIX + key

    def op():
        if cache.add(full, 1, window_seconds):
            return 1
        try:
            return cache.incr(full)
        except ValueError:                       # بین add و incr منقضی شد
            cache.set(full, 1, window_seconds)
            return 1

    return _call(op)


def acquire(key, ttl_seconds):
    """ قفل/cooldown: True اگر همین الان گرفته شد، False اگر قبلاً بوده، None اگر Redis قطع است """
    result = _call(lambda: cache.add(PREFIX + key, 1, ttl_seconds))
    return result


def mark(key, ttl_seconds):
    _call(lambda: cache.set(PREFIX + key, 1, ttl_seconds))


def exists(key):
    """ ← True/False یا None (قطع) """
    return _call(lambda: cache.get(PREFIX + key) is not None)


def mark_customer_seen(conversation_id, ttl_seconds=60):
    mark(f'seen:{conversation_id}', ttl_seconds)


def customer_seen_recently(conversation_id):
    return exists(f'seen:{conversation_id}')
