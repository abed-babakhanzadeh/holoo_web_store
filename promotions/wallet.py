"""
کیف کدهای تخفیف کاربر (پنل کاربری): «کدهای من» و «دریافت کد جدید».

ایزولاسیون (اصل اول این ماژول):
  - «کدهای من» فقط از UserCoupon/CouponRedemptionِ *همان کاربر* ساخته می‌شود؛ هیچ ورودیِ شناسه‌ای ندارد.
  - صفحه‌ی «دریافت کد» فقط کدهای `is_claimable` (مخاطب «همه») را فهرست می‌کند و *متنِ کد را نشان نمی‌دهد*؛ کد فقط پس از
    دریافتِ موفق به کیفِ همان کاربر می‌آید. کدِ تخصیصی/اختصاصی (audience=assigned) هرگز در این صفحه یا با endpoint دریافت
    قابل‌کشف نیست: برای آن‌ها پاسخ دریافت با کدِ ناموجود یکسان است.
  - دریافت داخل transaction.atomic با قفل ردیف کوپن انجام می‌شود؛ یکتایی (کوپن، کاربر) و سقف دریافت‌کنندگان
    (claim_limit) و سقف کل مصرف داخل همان قفل سنجیده می‌شود، پس دریافتِ هم‌زمان هیچ‌وقت از ظرفیت عبور نمی‌کند.

وضعیت هر کدِ کیف (coupon_state) به‌ترتیب: غیرفعال ← منقضی ← زمان‌بندی‌شده ← ظرفیت تمام ← سهم کاربر تمام ← فعال.
تب «فعال» = فعال + زمان‌بندی‌شده؛ تب «منقضی/تمام‌شده» = بقیه؛ تب «استفاده‌شده» = مصرف‌های خودِ کاربر (رزرو/نهایی).
"""

import logging
from dataclasses import dataclass, field
from decimal import Decimal

import jdatetime
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from accounts.stats import get as get_stat

from .models import Coupon, CouponRedemption, UserCoupon
from .resolver import _loyalty_index

logger = logging.getLogger(__name__)

STATE_ACTIVE = 'active'
STATE_SCHEDULED = 'scheduled'
STATE_EXPIRED = 'expired'
STATE_INACTIVE = 'inactive'
STATE_EXHAUSTED = 'exhausted'
STATE_USED_UP = 'used_up'

STATE_LABELS = {
    STATE_ACTIVE: 'قابل استفاده',
    STATE_SCHEDULED: 'به‌زودی فعال می‌شود',
    STATE_EXPIRED: 'منقضی شده',
    STATE_INACTIVE: 'غیرفعال شده',
    STATE_EXHAUSTED: 'ظرفیت تمام شد',
    STATE_USED_UP: 'استفاده شد',
}
LIVE_STATES = (STATE_ACTIVE, STATE_SCHEDULED)

# رنگ کارت به‌ازای نوع کد (کلاس‌های باندل Arino)
GRADIENTS = {
    Coupon.KIND_PERCENT: 'from-primary to-secondary',
    Coupon.KIND_FIXED: 'from-success to-emerald-400',
    Coupon.KIND_FREE_SHIPPING: 'from-warning to-amber-400',
}

# نتیجه‌ی دریافت
CLAIMED = 'claimed'
ALREADY = 'already'
FULL = 'full'
UNAVAILABLE = 'unavailable'

CLAIM_MESSAGES = {
    CLAIMED: 'کد تخفیف به «کدهای من» شما اضافه شد.',
    ALREADY: 'این کد قبلاً برای شما دریافت شده است.',
    FULL: 'ظرفیت دریافت این کد تکمیل شده است.',
    # یکسان برای کدِ ناموجود، غیرقابل‌دریافت، اختصاصی، منقضی و واجد‌شرایط‌نبودن؛ تا چیزی درباره‌ی کدهای دیگر فاش نشود
    UNAVAILABLE: 'این کد در حال حاضر قابل دریافت نیست.',
}


def jalali_date(value):
    if not value:
        return ''
    if timezone.is_aware(value):
        value = timezone.localtime(value)
    return jdatetime.datetime.fromgregorian(datetime=value).strftime('%Y/%m/%d')


def _money(amount):
    return f'{int(amount):,} تومان'


# ---------------------------------------------------------------- قوانین (متن قابل‌نمایش)
def coupon_rules(coupon):
    """ فهرست بندهای «قوانین» یک کد: شرط‌های خودکار از فیلدها + بندهای دلخواه ادمین (terms) """
    rules = []
    if coupon.kind == Coupon.KIND_PERCENT:
        cap = f' (حداکثر {_money(coupon.max_discount_amount)})' if coupon.max_discount_amount else ''
        rules.append(f'تخفیف {coupon.value}٪ روی مبلغ کالاهای مشمول{cap}.')
    elif coupon.kind == Coupon.KIND_FIXED:
        rules.append(f'{_money(coupon.value)} تخفیف روی مبلغ کالاها.')
    else:
        rules.append('هزینه‌ی ارسال با پیک درون‌شهری رایگان می‌شود.')

    if coupon.is_item_kind:
        if coupon.scope == Coupon.SCOPE_CART:
            rules.append('روی همه‌ی کالاهای سبد اعمال می‌شود.')
        else:
            # .all() (نه values_list/count) تا با prefetch_related در my_codes/claimable_coupons کوئری اضافه نزند
            products, categories = list(coupon.products.all()), list(coupon.categories.all())
            names = [p.name for p in products[:4]] + [c.name for c in categories[:4]]
            total = len(products) + len(categories)
            rules.append('فقط برای این کالاها/دسته‌ها: ' + '، '.join(names) + (f' و {total - len(names)} مورد دیگر' if total > len(names) else '') + '.')
        if not coupon.allow_with_promotions:
            rules.append('روی کالاهای دارای تخفیف خودکار اعمال نمی‌شود.')
    if coupon.min_cart_amount:
        rules.append(f'حداقل مبلغ سبد: {_money(coupon.min_cart_amount)} (پس از کسر تخفیف‌های خودکار).')
    if coupon.starts_at and coupon.ends_at:
        rules.append(f'اعتبار: از {jalali_date(coupon.starts_at)} تا {jalali_date(coupon.ends_at)}.')
    elif coupon.ends_at:
        rules.append(f'اعتبار تا {jalali_date(coupon.ends_at)}.')
    elif coupon.starts_at:
        rules.append(f'اعتبار از {jalali_date(coupon.starts_at)}.')
    if coupon.per_user_limit is not None:
        rules.append('هر کاربر فقط یک بار می‌تواند استفاده کند.' if coupon.per_user_limit == 1
                     else f'هر کاربر حداکثر {coupon.per_user_limit} بار می‌تواند استفاده کند.')
    if coupon.first_order_only:
        rules.append('فقط برای اولین خرید شما.')
    if coupon.min_loyalty_level:
        label = dict(Coupon._meta.get_field('min_loyalty_level').choices).get(coupon.min_loyalty_level, '')
        rules.append(f'ویژه‌ی {label}.')
    rules.extend(line.strip() for line in (coupon.terms or '').splitlines() if line.strip())
    return rules


def audience_badge(coupon):
    if coupon.min_loyalty_level:
        return dict(Coupon._meta.get_field('min_loyalty_level').choices).get(coupon.min_loyalty_level, 'ویژه')
    if coupon.audience == Coupon.AUDIENCE_ASSIGNED:
        return 'اختصاصی شما'
    return 'همگانی'


# ---------------------------------------------------------------- وضعیت
def coupon_state(coupon, total_uses, user_uses, now):
    if not coupon.is_active:
        return STATE_INACTIVE
    if coupon.ends_at and now > coupon.ends_at:
        return STATE_EXPIRED
    if coupon.starts_at and now < coupon.starts_at:
        return STATE_SCHEDULED
    if coupon.total_limit is not None and total_uses >= coupon.total_limit:
        return STATE_EXHAUSTED
    if coupon.per_user_limit is not None and user_uses >= coupon.per_user_limit:
        return STATE_USED_UP
    return STATE_ACTIVE


def _use_counts(coupon_ids, now, user=None):
    """ تعداد مصرف‌های شمرده‌شونده در ظرفیت (نهایی + رزرو معتبر؛ همان تعریف coupons.active_uses) به‌ازای هر کوپن """
    live = Q(status=CouponRedemption.STATUS_REDEEMED) | Q(status=CouponRedemption.STATUS_RESERVED, expires_at__gt=now)
    queryset = CouponRedemption.objects.filter(coupon_id__in=coupon_ids).filter(live)
    if user is not None:
        queryset = queryset.filter(user=user)
    return dict(queryset.values('coupon_id').annotate(n=Count('id')).values_list('coupon_id', 'n'))


@dataclass(frozen=True)
class WalletCode:
    coupon: object = field(compare=False)
    code: str
    title: str
    kind: str
    value_display: str
    state: str
    state_label: str
    badge: str
    starts_at: object
    ends_at: object
    min_cart_amount: int
    rules: tuple
    source: str
    received_at: object

    @property
    def is_live(self):
        return self.state in LIVE_STATES

    @property
    def gradient(self):
        return GRADIENTS.get(self.kind, GRADIENTS[Coupon.KIND_PERCENT])


@dataclass(frozen=True)
class UsedRow:
    code: str
    coupon_title: str
    value_display: str
    kind: str
    order_id: int
    order_total: object
    used_at: object
    saving: Decimal
    status: str
    status_label: str
    has_order: bool


@dataclass(frozen=True)
class ClaimCard:
    """ کارتِ صفحه‌ی «دریافت کد»؛ عمداً *بدون* متن کد """
    pk: int
    title: str
    kind: str
    value_display: str
    badge: str
    ends_at: object
    min_cart_amount: int
    rules: tuple
    remaining: object            # ظرفیت باقی‌مانده‌ی دریافت (None = نامحدود)


def my_codes(user, now=None):
    """ {'active': [...], 'expired': [...], 'used': [...]} فقط برای همین کاربر (هیچ ورودی شناسه‌ای ندارد) """
    now = now or timezone.now()
    assignments = list(UserCoupon.objects.filter(user=user).select_related('coupon')
                       .prefetch_related('coupon__products', 'coupon__categories').order_by('-created_at', '-id'))
    ids = [a.coupon_id for a in assignments]
    total_uses = _use_counts(ids, now) if ids else {}
    user_uses = _use_counts(ids, now, user) if ids else {}

    active, expired = [], []
    for assignment in assignments:
        coupon = assignment.coupon
        state = coupon_state(coupon, total_uses.get(coupon.pk, 0), user_uses.get(coupon.pk, 0), now)
        code = WalletCode(
            coupon=coupon, code=coupon.code, title=coupon.title, kind=coupon.kind, value_display=coupon.short_display, state=state,
            state_label=STATE_LABELS[state], badge=audience_badge(coupon), starts_at=coupon.starts_at, ends_at=coupon.ends_at,
            min_cart_amount=coupon.min_cart_amount, rules=tuple(coupon_rules(coupon)), source=assignment.source,
            received_at=assignment.created_at,
        )
        (active if code.is_live else expired).append(code)

    redemptions = list(
        CouponRedemption.objects.filter(user=user, status__in=[CouponRedemption.STATUS_RESERVED, CouponRedemption.STATUS_REDEEMED])
        .select_related('coupon').order_by('-reserved_at', '-id')
    )
    used = _used_rows(user, redemptions)
    return {'active': active, 'expired': expired, 'used': used}


def _used_rows(user, redemptions):
    if not redemptions:
        return []
    # سفارش‌ها فقط برای نمایش مبلغ فاکتور و لینک؛ با فیلتر مالک (سفارشِ کاربر دیگر هرگز نمی‌آید). import تنبل: جهت وابستگی معکوس نشود
    from orders.models import Order
    orders = {o['pk']: o for o in Order.objects.filter(pk__in=[r.order_id for r in redemptions], user=user).values('pk', 'total_price')}
    rows = []
    for redemption in redemptions:
        order = orders.get(redemption.order_id)
        rows.append(UsedRow(
            code=redemption.code, coupon_title=redemption.coupon.title, value_display=redemption.coupon.short_display,
            kind=redemption.coupon.kind, order_id=redemption.order_id, order_total=order['total_price'] if order else None,
            used_at=redemption.redeemed_at or redemption.reserved_at,
            saving=redemption.discount_amount + redemption.shipping_discount, status=redemption.status,
            status_label=dict(CouponRedemption.STATUS_CHOICES)[redemption.status], has_order=order is not None,
        ))
    return rows


# ---------------------------------------------------------------- دریافت کد
def _claim_window_ok(coupon, now):
    return (coupon.is_active and coupon.is_claimable and coupon.audience == Coupon.AUDIENCE_EVERYONE
            and (coupon.starts_at is None or coupon.starts_at <= now) and (coupon.ends_at is None or now <= coupon.ends_at))


def _user_eligible(coupon, user):
    """ سطح وفاداری و «فقط اولین خرید» (نامعلوم بودن آمار = واجد شرایط نیست) """
    if coupon.min_loyalty_level and _loyalty_index(user) < coupon.min_loyalty_level:
        return False
    if coupon.first_order_only:
        placed = get_stat('orders_placed_count', user, None)
        if placed is None or placed > 0:
            return False
    return True


def _claim_counts(coupon_ids):
    return dict(UserCoupon.objects.filter(coupon_id__in=coupon_ids).values('coupon_id').annotate(n=Count('id')).values_list('coupon_id', 'n'))


def _leaks_code(coupon):
    """
    لایه‌ی دفاعیِ دوم (اعتبارسنجی اصلی در Coupon.clean است): کدِ قابل‌دریافتی که متنش در عنوان/قوانینش آمده (مثلاً با ذخیره‌ی
    مستقیم بدون فرم، یا داده‌ی قدیمی) هرگز فهرست نمی‌شود تا صفحه‌ی «دریافت» کد را فاش نکند.
    """
    leaking = coupon.leaking_fields()
    if leaking:
        logger.warning('کد قابل‌دریافت id=%s در فیلد %s متن خودش را دارد؛ در صفحه‌ی «دریافت» فهرست نشد.',
                       coupon.pk, '، '.join(leaking))
    return bool(leaking)


def claimable_coupons(user, now=None):
    """ کدهای قابل‌دریافتِ واجد شرایط برای کاربر (بدون متن کد) """
    now = now or timezone.now()
    owned = set(UserCoupon.objects.filter(user=user).values_list('coupon_id', flat=True))
    candidates = [
        c for c in Coupon.objects.filter(is_claimable=True, is_active=True, audience=Coupon.AUDIENCE_EVERYONE)
        .prefetch_related('products', 'categories').order_by('-created_at', '-id')
        if c.pk not in owned and _claim_window_ok(c, now) and _user_eligible(c, user) and not _leaks_code(c)
    ]
    if not candidates:
        return []
    ids = [c.pk for c in candidates]
    claims, uses = _claim_counts(ids), _use_counts(ids, now)
    cards = []
    for coupon in candidates:
        if coupon.claim_limit is not None and claims.get(coupon.pk, 0) >= coupon.claim_limit:
            continue
        if coupon.total_limit is not None and uses.get(coupon.pk, 0) >= coupon.total_limit:
            continue
        remaining = None if coupon.claim_limit is None else coupon.claim_limit - claims.get(coupon.pk, 0)
        cards.append(ClaimCard(
            pk=coupon.pk, title=coupon.title, kind=coupon.kind, value_display=coupon.short_display, badge=audience_badge(coupon),
            ends_at=coupon.ends_at, min_cart_amount=coupon.min_cart_amount, rules=tuple(coupon_rules(coupon)), remaining=remaining,
        ))
    return cards


@dataclass(frozen=True)
class ClaimResult:
    status: str
    code: str = ''
    message: str = ''
    coupon: object = field(default=None, compare=False)
    rules: tuple = ()

    @property
    def ok(self):
        return self.status in (CLAIMED, ALREADY)


def _result(status, coupon=None, reveal=False):
    return ClaimResult(status=status, message=CLAIM_MESSAGES[status], code=coupon.code if (coupon and reveal) else '',
                       coupon=coupon if reveal else None, rules=tuple(coupon_rules(coupon)) if (coupon and reveal) else ())


def claim(user, coupon_id, now=None):
    """
    دریافت اتمیک یک کد برای کاربر. متن کد فقط در نتیجه‌ی موفق (یا «قبلاً دریافت شده») برمی‌گردد.
    هر حالتِ غیرقابل‌دریافت (ناموجود، اختصاصی، منقضی، واجد شرایط نبودن) پیام یکسانِ UNAVAILABLE می‌دهد.
    """
    now = now or timezone.now()
    with transaction.atomic():
        found = list(Coupon.objects.select_for_update().filter(pk=coupon_id))     # قفل ردیف کوپن؛ list نه .first() (سازگار با SQL Server)
        coupon = found[0] if found else None
        if coupon is None or not _claim_window_ok(coupon, now):
            return _result(UNAVAILABLE)
        if UserCoupon.objects.filter(coupon=coupon, user=user).exists():
            return _result(ALREADY, coupon, reveal=True)
        if not _user_eligible(coupon, user):
            return _result(UNAVAILABLE)
        if coupon.claim_limit is not None and UserCoupon.objects.filter(coupon=coupon).count() >= coupon.claim_limit:
            return _result(FULL)
        if coupon.total_limit is not None and _use_counts([coupon.pk], now).get(coupon.pk, 0) >= coupon.total_limit:
            return _result(FULL)
        UserCoupon.objects.create(coupon=coupon, user=user, source=UserCoupon.SOURCE_CLAIMED)
        return _result(CLAIMED, coupon, reveal=True)
