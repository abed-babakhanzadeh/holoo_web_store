"""
محاسبه‌ی تخفیف خودکار روی «قیمت واحد» یک کالا (مرحله‌ی ۲ از ترتیب محاسبه‌ی قیمت؛ نگاه کنید products/pricing.py).

resolve_unit_price(product, user, method, base_price, now) -> (قیمت نهایی، (AppliedPromotion, ...))

قواعد (به ترتیب):
  ۱. سیاست سراسری: خاموش بودن تخفیف‌ها؛ کاربر ویژه (سطح قیمت ≥ ۳ یا روش پرداخت «ویژه») فقط اگر apply_to_vip؛
     روش نقدی/چکی فقط اگر apply_for_cash / apply_for_check.
  ۲. برای هر تخفیف: در بازه‌ی زمانی باشد، کالا با اهداف (شمول/استثنا) بخواند و شرایط مخاطب برقرار باشد
     (ورود، حداقل سطح وفاداری، سطوح قیمت، روش پرداخت).
  ۳. مبلغ تخفیف هر مورد: درصدی (با سقف مبلغ)، مبلغ ثابت، یا قیمت ویژه؛ سپس سقف سراسری درصد روی هر کالا.
  ۴. ترکیب: «بهترین» (بیشترین صرفه؛ تساوی ← اولویت بالاتر ← شناسه‌ی کمتر) یا «جمع‌شونده» (به‌ترتیب اولویت روی قیمتِ
     جاری، با سقف کل).
  ۵. گرد کردن به نزدیک‌ترین مضرب rounding_step (هرگز بالاتر از قیمت پایه) و حذف تخفیفِ بی‌اثر.
"""

from decimal import Decimal, ROUND_HALF_UP

from products.pricing import AppliedPromotion, VIP, VIP_PRICE_LEVEL, _price_level

from accounts.models import CustomUser

from .index import get_index
from .models import Promotion

_ZERO = Decimal('0')
_HUNDRED = Decimal('100')


def _loyalty_index(user):
    """ اندیس سطح وفاداری کاربر در CustomUser.LOYALTY_LEVELS (برای مهمان: ۰)؛ فقط در صورت نیاز محاسبه می‌شود """
    if user is None or not getattr(user, 'is_authenticated', False):
        return 0
    orders = getattr(user, 'paid_orders_count', 0) or 0
    level = 0
    for index, (threshold, _label) in enumerate(CustomUser.LOYALTY_LEVELS):
        if orders >= threshold:
            level = index
    return level


def _policy_allows(policy, user, method):
    if not policy.promotions_enabled:
        return False
    is_vip = method == VIP or _price_level(user) >= VIP_PRICE_LEVEL
    if is_vip:
        return policy.apply_to_vip
    if method == 'cash':
        return policy.apply_for_cash
    return policy.apply_for_check


def _audience_ok(rule, user, method, loyalty_getter):
    authenticated = bool(user is not None and getattr(user, 'is_authenticated', False))
    if rule.login_required and not authenticated:
        return False
    if rule.min_loyalty_level and loyalty_getter() < rule.min_loyalty_level:
        return False
    if rule.price_levels and _price_level(user) not in rule.price_levels:
        return False
    if rule.payment_method and rule.payment_method != method:
        return False
    return True


def _raw_discount(rule, price):
    """ مبلغ تخفیف این تخفیف روی قیمت جاری (پیش از سقف سراسری) """
    if rule.kind == Promotion.KIND_PERCENT:
        amount = price * Decimal(rule.value) / _HUNDRED
        if rule.max_discount_amount:
            amount = min(amount, Decimal(rule.max_discount_amount))
        return amount
    if rule.kind == Promotion.KIND_FIXED:
        return min(Decimal(rule.value), price)
    if rule.kind == Promotion.KIND_SPECIAL_PRICE:
        return price - Decimal(rule.value) if price > Decimal(rule.value) else _ZERO
    return _ZERO


def _round_to_step(price, step):
    if step <= 1:
        return price.quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    step = Decimal(step)
    return (price / step).quantize(Decimal('1'), rounding=ROUND_HALF_UP) * step


def eligible_rules(index, product, user, method, now):
    """ تخفیف‌های فعال و مشمول این کالا/کاربر/روش پرداخت (پیش از محاسبه‌ی مبلغ) """
    if not _policy_allows(index.policy, user, method):
        return []
    loyalty_cache = []

    def loyalty():
        if not loyalty_cache:
            loyalty_cache.append(_loyalty_index(user))
        return loyalty_cache[0]

    return [
        rule for rule in index.rules
        if rule.in_window(now) and rule.matches_product(product) and _audience_ok(rule, user, method, loyalty)
    ]


def resolve_unit_price(product, user, method, base_price, now=None):
    from django.utils import timezone

    now = now or timezone.now()
    index = get_index()
    rules = eligible_rules(index, product, user, method, now)
    if not rules:
        return base_price, ()

    policy = index.policy
    max_total = base_price * Decimal(policy.max_item_discount_percent) / _HUNDRED

    if policy.stacking == 'stack':
        ordered = sorted(rules, key=lambda r: (-r.priority, r.id))
        price, applied = base_price, []
        for rule in ordered:
            room = max_total - (base_price - price)
            if room <= 0:
                break
            discount = min(_raw_discount(rule, price), room)
            if discount <= 0:
                continue
            price -= discount
            applied.append((rule, discount))
    else:
        scored = []
        for rule in rules:
            discount = min(_raw_discount(rule, base_price), max_total)
            if discount > 0:
                scored.append((discount, rule.priority, -rule.id, rule))
        if not scored:
            return base_price, ()
        discount, _priority, _neg_id, rule = max(scored, key=lambda item: item[:3])
        price, applied = base_price - discount, [(rule, discount)]

    if not applied:
        return base_price, ()

    final = _round_to_step(price, policy.rounding_step)
    if final > base_price:
        final = base_price
    if final < 0:
        final = _ZERO
    if final >= base_price:
        return base_price, ()

    total_discount = base_price - final
    raw_total = sum((d for _r, d in applied), _ZERO)
    scale = (total_discount / raw_total) if raw_total else _ZERO      # توزیع اثر گرد کردن روی تخفیف‌های اعمال‌شده
    applied_promotions = tuple(
        AppliedPromotion(
            promotion_id=rule.id, title=rule.title, kind=rule.kind, value=rule.value,
            discount=(discount * scale).quantize(Decimal('1'), rounding=ROUND_HALF_UP),
            badge_label=rule.badge_label, ends_at=rule.ends_at,
        )
        for rule, discount in applied
    )
    return final, applied_promotions
