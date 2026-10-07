"""
دسترسی گفتگوی زنده. فاز ۲: حضور کارشناس (Presence) هنوز ساخته نشده، پس «کارشناس آنلاین» همیشه False است؛ فاز ۳ این تابع را با
Redis + OperatorPresence پر می‌کند. فقط دروازه‌ی ساخت گفتگوی زنده (T1) و بازگشت به صف (T11) از آن می‌پرسند؛ هرگز وضعیت
گفتگوی جاری را عوض نمی‌کند.
"""
from django.utils import timezone

from products.chat_settings import current_hours_state


def operator_available(now=None):
    return False


def live_available(cfg, now=None):
    now = now or timezone.now()
    return bool(current_hours_state(cfg, now)['in_hours'] and operator_available(now))
