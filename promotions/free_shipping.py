"""
اسنپ‌شات و لودر قاعده‌های ارسال رایگان (FreeShippingRule).

orders/shipping.py::shipping_quote تابعی خالص است و به این اپ وابسته نیست؛ آنچه از این ماژول می‌گیرد «اسنپ‌شات‌های خالص»
است که با duck-typing این واسط را دارند: matches(city, cart_total, now)، label()، post_label()، covers_postage، id.

مثل شاخص تخفیف‌ها، قاعده‌ها برای هر درخواست از دیتابیس خوانده نمی‌شوند: حافظه‌ی پروسه (چند ثانیه) ← Redis (۵ دقیقه، کلید
شامل نام دیتابیس) ← دیتابیس. بازه‌ی زمانی هنگام ارزیابی (نه هنگام ساخت) با ساعت سرور سنجیده می‌شود، پس گذر زمان به باطل‌سازی
نیاز ندارد. با هر ذخیره/حذف قاعده و هر تغییر استان/شهرهای آن کش پاک می‌شود (promotions/signals.py).
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta

from django.core.cache import cache
from django.db import connection
from django.utils import timezone

from .models import FreeShippingRule

logger = logging.getLogger(__name__)

REDIS_TTL = 5 * 60
MEMO_TTL = 2.0
EXPIRY_GRACE = timedelta(hours=1)          # مثل شاخص تخفیف‌ها؛ نگاه کنید promotions/index.py

_memo = {'rules': None, 'at': 0.0}


@dataclass(frozen=True)
class FreeShippingRuleSnapshot:
    id: int
    title: str
    min_total: int
    starts_at: object
    ends_at: object
    priority: int
    province_ids: frozenset
    city_ids: frozenset
    postage_mode: str
    built_at: object = field(default=None, compare=False)

    @property
    def covers_postage(self):
        return self.postage_mode == FreeShippingRule.POSTAGE_COVER

    @property
    def nationwide(self):
        return not self.province_ids and not self.city_ids

    def in_window(self, now):
        return (self.starts_at is None or self.starts_at <= now) and (self.ends_at is None or now <= self.ends_at)

    def covers_city(self, city):
        """ کل کشور، یا شهرِ آدرس در «شهرها»، یا استانش در «استان‌ها» """
        if self.nationwide:
            return True
        return city.pk in self.city_ids or city.province_id in self.province_ids

    def matches(self, city, cart_total, now):
        return self.in_window(now) and self.covers_city(city) and cart_total >= self.min_total

    def _threshold(self):
        return f'خرید بالای {self.min_total} تومان' if self.min_total else self.title

    def label(self):
        return f'ارسال رایگان ({self._threshold()})'

    def post_label(self):
        return f'ارسال رایگان با پست ({self._threshold()})'


def build_rules(now=None):
    """ قاعده‌های فعال را از دیتابیس می‌خواند (سه کوئری ثابت) و به‌ترتیب اولویت برمی‌گرداند """
    now = now or timezone.now()
    from django.db.models import Q
    rules = list(FreeShippingRule.objects.filter(is_active=True).filter(Q(ends_at__isnull=True) | Q(ends_at__gte=now - EXPIRY_GRACE)))
    if not rules:
        return ()
    ids = [rule.pk for rule in rules]
    provinces, cities = {}, {}
    for rule_id, province_id in FreeShippingRule.provinces.through.objects.filter(freeshippingrule_id__in=ids).values_list('freeshippingrule_id', 'province_id'):
        provinces.setdefault(rule_id, set()).add(province_id)
    for rule_id, city_id in FreeShippingRule.cities.through.objects.filter(freeshippingrule_id__in=ids).values_list('freeshippingrule_id', 'city_id'):
        cities.setdefault(rule_id, set()).add(city_id)
    snapshots = [
        FreeShippingRuleSnapshot(
            id=rule.pk, title=rule.title, min_total=rule.min_cart_total, starts_at=rule.starts_at, ends_at=rule.ends_at,
            priority=rule.priority, province_ids=frozenset(provinces.get(rule.pk, ())), city_ids=frozenset(cities.get(rule.pk, ())),
            postage_mode=rule.postage_mode, built_at=now,
        )
        for rule in rules
    ]
    snapshots.sort(key=lambda r: (-r.priority, r.id))
    return tuple(snapshots)


def get_config():
    """
    سیاست سراسری ارسال رایگان: (قاعده‌ها فعال‌اند؟، حداقل مبلغ پس از کد تخفیفِ کالا هم سنجیده شود؟).
    از همان شاخص کش‌شده‌ی سیاست می‌آید (بدون کوئری اضافه) و با هر ذخیره‌ی سیاست باطل می‌شود.
    """
    from .index import get_index
    policy = get_index().policy
    return policy.free_shipping_rules_enabled, policy.free_shipping_threshold_after_coupon


def enabled_rules():
    """ قاعده‌های فعال‌شده‌ی قابل‌اعمال: با کلید سراسری خاموش، هیچ قاعده‌ای (فهرست خالی) """
    return get_rules() if get_config()[0] else ()


def cache_key():
    return f'promotions:freeship:v1:{connection.settings_dict["NAME"]}'


def get_rules():
    """ قاعده‌های فعال (حافظه ← Redis ← دیتابیس)؛ خطای Redis هرگز ارسال را نمی‌شکند """
    now_mono = time.monotonic()
    if _memo['rules'] is not None and now_mono - _memo['at'] < MEMO_TTL:
        return _memo['rules']
    rules = None
    try:
        rules = cache.get(cache_key())
    except Exception:                                    # noqa: BLE001
        logger.warning('خواندن قاعده‌های ارسال رایگان از کش ناموفق بود؛ از دیتابیس ساخته می‌شود.', exc_info=True)
    if rules is None:
        rules = build_rules()
        try:
            cache.set(cache_key(), rules, REDIS_TTL)
        except Exception:                                # noqa: BLE001
            logger.warning('نوشتن قاعده‌های ارسال رایگان در کش ناموفق بود.', exc_info=True)
    _memo['rules'], _memo['at'] = rules, now_mono
    return rules


def invalidate():
    _memo['rules'], _memo['at'] = None, 0.0
    try:
        cache.delete(cache_key())
    except Exception:                                    # noqa: BLE001
        logger.warning('پاک‌کردن قاعده‌های ارسال رایگان از کش ناموفق بود.', exc_info=True)
