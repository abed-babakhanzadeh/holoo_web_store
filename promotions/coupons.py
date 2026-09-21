"""
سرویس کدهای تخفیف: ارزیابی، رزرو، مصرف نهایی و آزادسازی.

جایگاه در ترتیب محاسبه‌ی قیمت (نگاه کنید products/pricing.py): بعد از تخفیف‌های خودکار و روی «مبلغ کالاها»؛ سپس ارسال.

ارزیابی (evaluate_coupon) تابعی بدون اثر جانبی است و ساعت را فقط از سرور می‌گیرد. ترتیب بررسی‌ها:
  فعال بودن ← بازه‌ی زمانی ← مخاطب (کدِ تخصیصی) ← «فقط اولین خرید» ← حداقل مبلغ سبد (پس از تخفیف‌های خودکار)
  ← اقلام مشمول (شمول محصول/دسته + قاعده‌ی ترکیب با تخفیف خودکار) ← محاسبه‌ی مبلغ ← سقف کل و سقف هر کاربر.

ظرفیت: «مصرف‌های نهایی + رزروهای هنوز معتبر». رزروِ منقضی خودبه‌خود نمی‌شمارد (حتی اگر تسک زمان‌بندی اجرا نشده باشد).
هم‌زمانی: ثبت سفارش ردیف کوپن را قفل می‌کند (select_for_update) و *داخل همان قفل* دوباره ارزیابی و رزرو می‌کند؛ پس
درخواست‌های هم‌زمان پشت هم صف می‌شوند و هیچ‌وقت از سقف عبور نمی‌کنند.

فلگ‌های DiscountPolicy برای تخفیف خودکار (apply_to_vip و ...) اینجا خوانده نمی‌شوند؛ فقط تنظیمات مخصوص کد
(مهلت رزرو، سقف تلاش) از همان مدل می‌آید.
"""

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, ROUND_FLOOR

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accounts.stats import get as get_stat

from .index import _expand_categories, category_children_map
from .models import Coupon, CouponRedemption, DiscountPolicy, UserCoupon, normalize_code
from .resolver import _loyalty_index

logger = logging.getLogger(__name__)

SESSION_KEY = 'coupon_code'

# --- کد خطاها ---
NOT_FOUND = 'not_found'
INACTIVE = 'inactive'
NOT_STARTED = 'not_started'
EXPIRED = 'expired'
NOT_ASSIGNED = 'not_assigned'
NOT_ELIGIBLE = 'not_eligible'
FIRST_ORDER_ONLY = 'first_order_only'
FIRST_ORDER_UNKNOWN = 'first_order_unknown'
MIN_CART = 'min_cart'
NOT_APPLICABLE = 'not_applicable'
NO_COMBINE = 'no_combine_with_promotions'
NEEDS_ADDRESS = 'needs_address'
SHIPPING_NOT_APPLICABLE = 'shipping_not_applicable'
SHIPPING_ALREADY_FREE = 'shipping_already_free'
EXHAUSTED = 'exhausted'
USER_LIMIT = 'user_limit'
EMPTY_CODE = 'empty_code'

# فقط اینها «حدس کد» حساب می‌شوند و در محدودکننده‌ی تلاش شمرده می‌شوند
GUESS_ERRORS = frozenset({NOT_FOUND, INACTIVE, NOT_STARTED, EXPIRED, NOT_ASSIGNED})

MESSAGES = {
    EMPTY_CODE: 'کد تخفیف را وارد کنید.',
    NOT_FOUND: 'کد تخفیف معتبر نیست.',
    INACTIVE: 'این کد تخفیف فعال نیست.',
    NOT_STARTED: 'زمان استفاده از این کد هنوز شروع نشده است.',
    EXPIRED: 'مهلت استفاده از این کد تخفیف به پایان رسیده است.',
    NOT_ASSIGNED: 'این کد تخفیف برای حساب شما تعریف نشده است.',
    NOT_ELIGIBLE: 'سطح وفاداری شما برای استفاده از این کد کافی نیست.',
    FIRST_ORDER_ONLY: 'این کد فقط برای اولین خرید شما قابل استفاده است.',
    FIRST_ORDER_UNKNOWN: 'در حال حاضر امکان بررسی این کد وجود ندارد؛ کمی بعد دوباره تلاش کنید.',
    NOT_APPLICABLE: 'هیچ‌یک از کالاهای سبد شما مشمول این کد تخفیف نیست.',
    NO_COMBINE: 'این کد با کالاهای دارای تخفیف خودکار قابل ترکیب نیست و همه‌ی کالاهای سبد شما از تخفیف خودکار برخوردارند.',
    NEEDS_ADDRESS: 'برای استفاده از این کد ابتدا آدرس تحویل را انتخاب کنید.',
    SHIPPING_NOT_APPLICABLE: 'این کد فقط برای ارسال با پیک درون‌شهری قابل استفاده است.',
    SHIPPING_ALREADY_FREE: 'ارسال این سفارش از قبل رایگان است و نیازی به این کد نیست.',
    EXHAUSTED: 'ظرفیت استفاده از این کد تخفیف به پایان رسیده است.',
    USER_LIMIT: 'شما قبلاً از این کد تخفیف استفاده کرده‌اید.',
}


@dataclass(frozen=True)
class CouponResult:
    ok: bool
    code: str = ''
    error: str = ''
    message: str = ''
    kind: str = ''
    title: str = ''
    item_discount: Decimal = Decimal('0')       # تخفیف کالاها (ریال)
    shipping_discount: Decimal = Decimal('0')   # کرایه‌ی بخشیده‌شده (کوپن ارسال رایگان)
    free_shipping: bool = False
    eligible_total: Decimal = Decimal('0')      # مبلغ اقلامی که کد روی آن‌ها اعمال شد
    excluded_lines: int = 0                     # ردیف‌های دارای تخفیف خودکار که کد رویشان اعمال نشد
    coupon: object = field(default=None, compare=False)

    @property
    def is_guess_failure(self):
        return self.error in GUESS_ERRORS

    @property
    def order_label(self):
        return f'کد تخفیف {self.code}' if self.code else ''


def _fail(error, coupon=None, code='', message=None):
    return CouponResult(ok=False, code=code or (coupon.code if coupon else ''), error=error,
                        message=message or MESSAGES[error], coupon=coupon)


# ---------------------------------------------------------------- جست‌وجو و شمارش
def find_coupon(raw_code, lock=False):
    """
    کد را (بی‌حساس به حروف و نوع ارقام) پیدا می‌کند. lock=True فقط داخل transaction.atomic: ردیف کوپن را قفل
    می‌کند. عمداً با list() خوانده می‌شود (نه .first()) چون روی SQL Server قفل با TOP/LIMIT سازگار نیست.
    """
    code = normalize_code(raw_code)
    if not code:
        return None
    queryset = Coupon.objects.filter(code=code)
    if lock:
        queryset = queryset.select_for_update()
    found = list(queryset)
    return found[0] if found else None


def active_uses(coupon, user=None, now=None, exclude_pk=None):
    """ مصرف‌های نهایی + رزروهای هنوز معتبر (رزرو منقضی نمی‌شمارد) """
    now = now or timezone.now()
    queryset = CouponRedemption.objects.filter(coupon=coupon).filter(
        Q(status=CouponRedemption.STATUS_REDEEMED)
        | Q(status=CouponRedemption.STATUS_RESERVED, expires_at__gt=now)
    )
    if user is not None:
        queryset = queryset.filter(user=user)
    if exclude_pk is not None:
        queryset = queryset.exclude(pk=exclude_pk)
    return queryset.count()


# ---------------------------------------------------------------- ارزیابی
def _matching_lines(coupon, lines):
    """ ردیف‌هایی که در شمول محصول/دسته‌ی کد می‌گنجند (شمول «کل سبد» = همه) """
    if coupon.scope == Coupon.SCOPE_CART:
        return list(lines)
    product_ids = set(coupon.products.values_list('pk', flat=True))
    category_ids = set(coupon.categories.values_list('pk', flat=True))
    if category_ids:
        category_ids = set(_expand_categories(category_ids, category_children_map()))
    return [line for line in lines if line.product.pk in product_ids or line.product.category_id in category_ids]


def _percent_of(amount, percent):
    return (amount * Decimal(percent) / Decimal(100)).to_integral_value(rounding=ROUND_FLOOR)


def evaluate_coupon(coupon, user, pricing, *, base_quote=None, now=None):
    """
    کد را روی قیمت‌گذاری سبد (cart.pricing.CartPricing) ارزیابی می‌کند.

    base_quote فقط برای کد «ارسال رایگان» لازم است: quoteِ ارسالِ *بدون* کوپن (تا معلوم شود پیکِ دارای کرایه است).
    خروجی CouponResult است؛ ok=False همراه با error/message مشخص.
    """
    now = now or timezone.now()
    if coupon is None:
        return _fail(NOT_FOUND)
    if not coupon.is_active:
        return _fail(INACTIVE, coupon)
    if coupon.starts_at and now < coupon.starts_at:
        return _fail(NOT_STARTED, coupon)
    if coupon.ends_at and now > coupon.ends_at:
        return _fail(EXPIRED, coupon)

    if coupon.audience == Coupon.AUDIENCE_ASSIGNED:
        if user is None or not UserCoupon.objects.filter(coupon=coupon, user=user).exists():
            return _fail(NOT_ASSIGNED, coupon)

    if coupon.min_loyalty_level and _loyalty_index(user) < coupon.min_loyalty_level:
        return _fail(NOT_ELIGIBLE, coupon)

    if coupon.first_order_only:
        placed = get_stat('orders_placed_count', user, None)
        if placed is None:
            return _fail(FIRST_ORDER_UNKNOWN, coupon)
        if placed > 0:
            return _fail(FIRST_ORDER_ONLY, coupon)

    if pricing.items_total < coupon.min_cart_amount:
        return _fail(MIN_CART, coupon, message=f'حداقل مبلغ سبد برای این کد {coupon.min_cart_amount} تومان است '
                                               f'(پس از کسر تخفیف‌های خودکار).')

    item_discount = Decimal('0')
    shipping_discount = Decimal('0')
    eligible_total = Decimal('0')
    excluded = 0

    if coupon.is_item_kind:
        lines = _matching_lines(coupon, pricing.lines)
        if not coupon.allow_with_promotions:
            kept = [line for line in lines if not line.has_discount]
            excluded = len(lines) - len(kept)
            lines = kept
        eligible_total = sum((line.total for line in lines), Decimal('0'))
        if eligible_total <= 0:
            return _fail(NO_COMBINE if excluded else NOT_APPLICABLE, coupon)
        if coupon.kind == Coupon.KIND_PERCENT:
            item_discount = _percent_of(eligible_total, coupon.value)
            if coupon.max_discount_amount:
                item_discount = min(item_discount, Decimal(coupon.max_discount_amount))
        else:
            item_discount = Decimal(coupon.value)
        item_discount = min(item_discount, eligible_total)
        if item_discount <= 0:
            return _fail(NOT_APPLICABLE, coupon)
    else:
        if base_quote is None or not base_quote.available:
            return _fail(NEEDS_ADDRESS, coupon)
        if base_quote.method != 'courier':
            return _fail(SHIPPING_NOT_APPLICABLE, coupon)
        if base_quote.cost <= 0:
            return _fail(SHIPPING_ALREADY_FREE, coupon)
        shipping_discount = Decimal(base_quote.cost)

    if coupon.total_limit is not None and active_uses(coupon, now=now) >= coupon.total_limit:
        return _fail(EXHAUSTED, coupon)
    if coupon.per_user_limit is not None and user is not None and active_uses(coupon, user, now) >= coupon.per_user_limit:
        return _fail(USER_LIMIT, coupon)

    return CouponResult(
        ok=True, code=coupon.code, kind=coupon.kind, title=coupon.title, item_discount=item_discount,
        shipping_discount=shipping_discount, free_shipping=not coupon.is_item_kind, eligible_total=eligible_total,
        excluded_lines=excluded, coupon=coupon,
    )


# ---------------------------------------------------------------- رزرو / مصرف / آزادسازی
def reserve(coupon, user, order_id, result, now=None):
    """ ظرفیت کد را برای یک سفارش رزرو می‌کند (باید داخل همان تراکنشی باشد که ردیف کوپن را قفل کرده) """
    now = now or timezone.now()
    minutes = DiscountPolicy.load().coupon_reservation_minutes
    return CouponRedemption.objects.create(
        coupon=coupon, user=user, order_id=order_id, code=coupon.code, status=CouponRedemption.STATUS_RESERVED,
        discount_amount=result.item_discount, shipping_discount=result.shipping_discount,
        reserved_at=now, expires_at=now + timedelta(minutes=minutes),
    )


def redeem_for_order(order_id, now=None):
    """
    پرداخت موفق: رزرو ← مصرف نهایی. اگر رزرو در فاصله‌ی ثبت تا پرداخت منقضی/آزاد شده باشد، سفارش با همان قیمتِ
    توافق‌شده پذیرفته می‌شود (مشتری پرداخت کرده) و در صورت خارج‌شدن از سقف، over_limit برای مدیر علامت می‌خورد.
    """
    now = now or timezone.now()
    with transaction.atomic():
        found = list(CouponRedemption.objects.filter(order_id=order_id))
        if not found:
            return None
        coupon = list(Coupon.objects.select_for_update().filter(pk=found[0].coupon_id))[0]     # قفل کوپن؛ سپس ردیف مصرف
        redemption = list(CouponRedemption.objects.select_for_update().filter(pk=found[0].pk))[0]
        if redemption.status == CouponRedemption.STATUS_REDEEMED:
            return redemption
        # رزروِ سالم (منقضی/آزاد نشده) خودش جزو ظرفیت است؛ فقط رزروِ کهنه باید ظرفیتِ باقی‌مانده را دوباره بسنجد
        stale = redemption.status == CouponRedemption.STATUS_RELEASED or bool(redemption.expires_at and redemption.expires_at <= now)
        over = False
        if stale:
            others = active_uses(coupon, now=now, exclude_pk=redemption.pk)
            others_user = active_uses(coupon, redemption.user, now, exclude_pk=redemption.pk) if redemption.user_id else 0
            over = bool(
                (coupon.total_limit is not None and others >= coupon.total_limit)
                or (coupon.per_user_limit is not None and others_user >= coupon.per_user_limit)
            )
        redemption.status = CouponRedemption.STATUS_REDEEMED
        redemption.redeemed_at = now
        redemption.released_at = None
        redemption.release_reason = ''
        redemption.over_limit = over
        if over:
            logger.warning('کد %s برای سفارش %s پس از پایان مهلت رزرو و خارج از سقف پرداخت شد.', coupon.code, order_id)
        redemption.save()
        return redemption


def release_for_order(order_id, reason='order_canceled', now=None):
    """ لغو سفارش: ظرفیت کد آزاد می‌شود (چه رزرو باشد چه مصرف‌شده). خروجی: تعداد ردیف آزادشده """
    now = now or timezone.now()
    with transaction.atomic():
        found = list(CouponRedemption.objects.filter(order_id=order_id).exclude(status=CouponRedemption.STATUS_RELEASED))
        if not found:
            return 0
        list(Coupon.objects.select_for_update().filter(pk=found[0].coupon_id))                 # هم‌ترتیب با بقیه‌ی مسیرها: اول کوپن
        return CouponRedemption.objects.filter(pk__in=[r.pk for r in found]).update(
            status=CouponRedemption.STATUS_RELEASED, released_at=now, release_reason=reason,
        )


def release_expired(now=None):
    """ رزروهای پرداخت‌نشده‌ی از مهلت گذشته را آزاد (وضعیت‌شان را «آزادشده») می‌کند؛ خروجی: تعداد """
    now = now or timezone.now()
    return CouponRedemption.objects.filter(status=CouponRedemption.STATUS_RESERVED, expires_at__lte=now).update(
        status=CouponRedemption.STATUS_RELEASED, released_at=now, release_reason='expired',
    )
