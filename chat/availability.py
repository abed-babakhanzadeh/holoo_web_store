"""
دروازه‌ی گفتگوی زنده. سه وضعیت برای بازدیدکننده:
  ۱) ساعت کاری + کارشناس آنلاین (یا تنظیمِ «فقط وقتی کارشناس آنلاین است» خاموش) ← 'live'
  ۲) ساعت کاری ولی کارشناس آنلاین نیست                                          ← 'no_operator'
  ۳) خارج از ساعت کاری                                                           ← 'after_hours'
فقط دروازه‌ی ساخت گفتگوی زنده (T1) و بازگشت به صف (T11) از این‌جا می‌پرسند؛ هرگز وضعیت گفتگوی جاری را عوض نمی‌کند.
"""
from django.utils import timezone

from products.chat_settings import current_hours_state

from . import presence


def operator_available(now=None, cfg=None):
    return presence.operator_available(now, cfg)


def live_state(cfg, now=None, hours=None):
    """ ← 'live' | 'no_operator' | 'after_hours' """
    now = now or timezone.now()
    hours = hours or current_hours_state(cfg, now)
    if not hours['in_hours']:
        return 'after_hours'
    if cfg.chat_live_requires_operator and not operator_available(now, cfg):
        return 'no_operator'
    return 'live'


def live_available(cfg, now=None):
    """ گفتگوی زنده فقط وقتی زبانه‌ی آن روشن است قابل شروع/بازگشت به صف است """
    return bool(cfg.chat_tab_live_enabled) and live_state(cfg, now) == 'live'
