"""
شاخص تخفیف‌های فعال (لودر + کش).

هر کارت محصول و هر ردیف سبد/فاکتور برای قیمت‌گذاری باید بداند «کدام تخفیف‌ها روی این کالا مشمول‌اند». اگر هر بار
از دیتابیس پرسیده می‌شد، صفحه‌ی پرمحصول یک کوئری به‌ازای هر کالا می‌زد. به‌جای آن، تمام تخفیف‌های فعال (و اهدافشان،
سیاست سراسری و درخت دسته‌ها) در چند کوئری ثابت خوانده و به‌صورت یک شاخص غیرقابل‌تغییر و درون‌حافظه‌ای نگه داشته می‌شود؛
تشخیص «این کالا مشمول کدام تخفیف است» فقط چند مقایسه‌ی مجموعه‌ای (O(1)) است و هیچ کوئری‌ای نمی‌زند.

لایه‌های کش:
  ۱. حافظه‌ی همین پروسه به‌مدت چند ثانیه (تا حلقه‌ی کارت‌ها به Redis هم نرود)
  ۲. Redis (پروسه‌های مختلف؛ TTL چند دقیقه). کلید شامل نام دیتابیس است تا تست‌ها/سرور توسعه که Redis مشترک دارند
     شاخص هم را آلوده نکنند.
  ۳. دیتابیس (چهار کوئری ثابت)

باطل‌سازی: هر ذخیره/حذف Promotion، PromotionTarget، DiscountPolicy و Category (تغییر درخت زیردسته‌ها) کش را پاک می‌کند
(promotions/signals.py). بازه‌ی زمانی شروع/پایان هنگام محاسبه (نه هنگام ساخت شاخص) سنجیده می‌شود، پس گذر زمان
به باطل‌سازی نیاز ندارد. خطای Redis هرگز قیمت‌گذاری را نمی‌شکند؛ به دیتابیس برمی‌گردد.
"""

import logging
import time
from dataclasses import dataclass, field

from django.core.cache import cache
from django.db import connection
from django.utils import timezone

from .models import DiscountPolicy, Promotion, PromotionTarget, parse_price_levels

logger = logging.getLogger(__name__)

REDIS_TTL = 5 * 60
MEMO_TTL = 2.0          # ثانیه

_memo = {'index': None, 'at': 0.0}


@dataclass(frozen=True)
class PolicySnapshot:
    promotions_enabled: bool
    apply_to_vip: bool
    apply_for_cash: bool
    apply_for_check: bool
    stacking: str
    max_item_discount_percent: int
    rounding_step: int


@dataclass(frozen=True)
class PromotionRule:
    """ نسخه‌ی غیرقابل‌تغییر و پیش‌محاسبه‌شده‌ی یک تخفیف (اهداف به مجموعه‌ی شناسه‌ها باز شده‌اند) """
    id: int
    title: str
    kind: str
    value: int
    max_discount_amount: object
    starts_at: object
    ends_at: object
    priority: int
    login_required: bool
    min_loyalty_level: int
    price_levels: frozenset
    payment_method: str
    show_in_flash_deals: bool
    badge_label: str
    include_all: bool
    include_products: frozenset
    include_categories: frozenset      # شامل زیردسته‌ها (در زمان ساخت باز شده)
    include_brands: frozenset
    exclude_products: frozenset
    exclude_categories: frozenset
    exclude_brands: frozenset

    @property
    def is_public(self):
        return not (self.login_required or self.min_loyalty_level or self.price_levels or self.payment_method)

    def in_window(self, now):
        return self.starts_at <= now <= self.ends_at

    def matches_product(self, product):
        """ کالا مشمول این تخفیف است؟ (فقط اهدافِ شمول/استثنا؛ شرایط مخاطب جدا بررسی می‌شود) """
        if product.pk in self.exclude_products:
            return False
        if product.category_id in self.exclude_categories:
            return False
        if product.brand_id is not None and product.brand_id in self.exclude_brands:
            return False
        if self.include_all:
            return True
        return (
            product.pk in self.include_products
            or product.category_id in self.include_categories
            or (product.brand_id is not None and product.brand_id in self.include_brands)
        )


@dataclass(frozen=True)
class PromotionIndex:
    policy: PolicySnapshot
    rules: tuple = ()
    built_at: object = field(default=None, compare=False)


# ---------------------------------------------------------------- ساخت شاخص از دیتابیس
def _descendants_map(parent_pairs):
    children = {}
    for category_id, parent_id in parent_pairs:
        children.setdefault(parent_id, []).append(category_id)
    return children


def _expand_categories(root_ids, children):
    """ شناسه‌ی دسته‌ها + همه‌ی نوادگانشان """
    seen, stack = set(), list(root_ids)
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(children.get(current, ()))
    return frozenset(seen)


def make_rule(promo, targets, children):
    """ یک Promotion + اهدافش را به PromotionRule غیرقابل‌تغییر (با دسته‌های باز‌شده به نوادگان) تبدیل می‌کند """
    include = {'products': set(), 'categories': set(), 'brands': set()}
    exclude = {'products': set(), 'categories': set(), 'brands': set()}
    include_all = False
    for target in targets:
        bucket = exclude if target.is_exclusion else include
        if target.target_type == PromotionTarget.TYPE_ALL:
            include_all = True
        elif target.target_type == PromotionTarget.TYPE_PRODUCT:
            if target.product_id is not None:
                bucket['products'].add(target.product_id)
        elif target.target_type == PromotionTarget.TYPE_CATEGORY:
            if target.category_id is None:
                continue
            ids = _expand_categories([target.category_id], children) if target.include_descendants else {target.category_id}
            bucket['categories'].update(ids)
        elif target.target_type == PromotionTarget.TYPE_BRAND:
            if target.brand_id is not None:
                bucket['brands'].add(target.brand_id)
    return PromotionRule(
        id=promo.pk, title=promo.title, kind=promo.kind, value=promo.value,
        max_discount_amount=promo.max_discount_amount, starts_at=promo.starts_at, ends_at=promo.ends_at,
        priority=promo.priority, login_required=promo.login_required, min_loyalty_level=promo.min_loyalty_level,
        price_levels=parse_price_levels(promo.price_levels), payment_method=promo.payment_method,
        show_in_flash_deals=promo.show_in_flash_deals, badge_label=promo.badge_label,
        include_all=include_all,
        include_products=frozenset(include['products']), include_categories=frozenset(include['categories']),
        include_brands=frozenset(include['brands']),
        exclude_products=frozenset(exclude['products']), exclude_categories=frozenset(exclude['categories']),
        exclude_brands=frozenset(exclude['brands']),
    )


def category_children_map():
    from products.models import Category
    return _descendants_map(Category.objects.values_list('id', 'parent_id'))


def build_index(now=None):
    """ شاخص را از دیتابیس می‌سازد (چهار کوئری ثابت، مستقل از تعداد کالا) """
    now = now or timezone.now()
    policy_obj = DiscountPolicy.load()
    policy = PolicySnapshot(
        promotions_enabled=policy_obj.promotions_enabled, apply_to_vip=policy_obj.apply_to_vip,
        apply_for_cash=policy_obj.apply_for_cash, apply_for_check=policy_obj.apply_for_check,
        stacking=policy_obj.promotion_stacking, max_item_discount_percent=policy_obj.max_item_discount_percent,
        rounding_step=policy_obj.rounding_step,
    )

    promotions = list(Promotion.objects.filter(is_active=True, ends_at__gte=now))
    targets_by_promotion = {}
    if promotions:
        for target in PromotionTarget.objects.filter(promotion_id__in=[p.pk for p in promotions]):
            targets_by_promotion.setdefault(target.promotion_id, []).append(target)
    children = category_children_map() if promotions else {}

    rules = [make_rule(promo, targets_by_promotion.get(promo.pk, ()), children) for promo in promotions]
    return PromotionIndex(policy=policy, rules=tuple(rules), built_at=now)


# ---------------------------------------------------------------- کش
def cache_key():
    return f'promotions:index:v1:{connection.settings_dict["NAME"]}'


def get_index():
    """ شاخص فعلی (حافظه ← Redis ← دیتابیس) """
    now_mono = time.monotonic()
    if _memo['index'] is not None and now_mono - _memo['at'] < MEMO_TTL:
        return _memo['index']

    index = None
    try:
        index = cache.get(cache_key())
    except Exception:                                    # noqa: BLE001 - قطعی Redis نباید قیمت‌گذاری را بشکند
        logger.warning('خواندن شاخص تخفیف از کش ناموفق بود؛ از دیتابیس ساخته می‌شود.', exc_info=True)
    if index is None:
        index = build_index()
        try:
            cache.set(cache_key(), index, REDIS_TTL)
        except Exception:                                # noqa: BLE001
            logger.warning('نوشتن شاخص تخفیف در کش ناموفق بود.', exc_info=True)

    _memo['index'], _memo['at'] = index, now_mono
    return index


def invalidate():
    """ کش شاخص را (حافظه + Redis) پاک می‌کند؛ با هر تغییرِ داده‌ی تخفیف صدا زده می‌شود """
    _memo['index'], _memo['at'] = None, 0.0
    try:
        cache.delete(cache_key())
    except Exception:                                    # noqa: BLE001
        logger.warning('پاک‌کردن شاخص تخفیف از کش ناموفق بود.', exc_info=True)
