"""
گزارش تحلیلی تخفیف‌ها برای مدیر: مصرف کوپن‌ها، مبلغ تخفیف‌داده‌شده، پرطرفدارترین کدها، تخفیف‌های خودکار و ارسال رایگان.

همه‌چیز با کوئری‌های تجمیعی (COUNT/SUM/GROUP BY) و در تعداد *ثابتِ* کوئری ساخته می‌شود، مستقل از تعداد سفارش/مصرف‌ها.

منبع داده:
  - کوپن‌ها: CouponRedemption (اسنپ‌شات مبلغ‌ها؛ وضعیت رزرو/مصرف‌شده/آزادشده).
  - تخفیف‌های خودکار: Order.promotion_discount و OrderItem.discount_amount. تخفیف خودکار روی سفارش «شناسه‌ی کمپین» ثبت
    نمی‌کند (اسنپ‌شات عمداً بدون FK است)؛ پس پرطرفدارترین «کمپین‌های خودکار» را از روی *کالاهای تخفیف‌خورده* می‌سنجیم.
  - ارسال رایگان: Order.shipping_discount؛ سهم کوپن از CouponRedemption.shipping_discount و بقیه = قاعده/پرچم کالا.

سفارش‌های لغوشده در همه‌ی جمع‌ها نمی‌آیند. بازه: آخرین N روز (۰ = از ابتدا).
"""

from datetime import timedelta

import jdatetime
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from .models import Coupon, CouponRedemption, UserCoupon

PERIODS = (7, 30, 90, 365, 0)
DEFAULT_DAYS = 30
TOP_N = 10
DAILY_DAYS = 14

_ZERO = 0
_MONEY = DecimalField(max_digits=16, decimal_places=0)


def normalize_days(raw):
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return days if days in PERIODS else DEFAULT_DAYS


def _sum(field, **kwargs):
    return Coalesce(Sum(field, **kwargs), _ZERO, output_field=_MONEY)


def build_report(days=DEFAULT_DAYS, now=None):
    """ دیکشنری ساده (قابل رندر و قابل تست) با کوئری‌های تجمیعیِ ثابت """
    from orders.models import Order, OrderItem            # import تنبل: promotions به orders وابستگی مدلی ندارد

    now = now or timezone.now()
    since = now - timedelta(days=days) if days else None

    redemptions = CouponRedemption.objects.all()
    orders = Order.objects.exclude(status='canceled')
    items = OrderItem.objects.exclude(order__status='canceled')
    claims = UserCoupon.objects.filter(source=UserCoupon.SOURCE_CLAIMED)
    if since:
        redemptions = redemptions.filter(reserved_at__gte=since)
        orders = orders.filter(created_at__gte=since)
        items = items.filter(order__created_at__gte=since)
        claims = claims.filter(created_at__gte=since)

    redeemed = Q(status=CouponRedemption.STATUS_REDEEMED)
    reserved = Q(status=CouponRedemption.STATUS_RESERVED, expires_at__gt=now)

    # ---- کوپن‌ها: یک کوئریِ تجمیعی برای همه‌ی شمارنده‌ها و مبلغ‌ها ----
    coupons_row = redemptions.aggregate(
        redeemed_count=Count('id', filter=redeemed),
        waiting_count=Count('id', filter=reserved),
        released_count=Count('id', filter=Q(status=CouponRedemption.STATUS_RELEASED) | Q(status=CouponRedemption.STATUS_RESERVED, expires_at__lte=now)),
        over_limit_count=Count('id', filter=Q(over_limit=True)),
        goods_discount=_sum('discount_amount', filter=redeemed),
        shipping_total=_sum('shipping_discount', filter=redeemed),
        waiting_discount=_sum(F('discount_amount') + F('shipping_discount'), filter=reserved),
        waiting_shipping=_sum('shipping_discount', filter=reserved),
        users=Count('user', filter=redeemed, distinct=True),
    )
    used = coupons_row['redeemed_count']
    coupons_row['total_discount'] = coupons_row['goods_discount'] + coupons_row['shipping_total']
    coupons_row['average_discount'] = int(coupons_row['total_discount'] / used) if used else 0

    live = redeemed | reserved
    top_rows = list(
        redemptions.filter(live).values('coupon_id', 'code', 'coupon__title', 'coupon__kind')
        .annotate(uses=Count('id'), discount=_sum(F('discount_amount') + F('shipping_discount')), users=Count('user', distinct=True))
        .order_by('-uses', '-discount', 'code')[:TOP_N]
    )
    top_by_discount = list(
        redemptions.filter(redeemed).values('coupon_id', 'code', 'coupon__title')
        .annotate(uses=Count('id'), discount=_sum(F('discount_amount') + F('shipping_discount')))
        .order_by('-discount', '-uses', 'code')[:TOP_N]
    )
    by_kind = {
        row['coupon__kind']: row for row in redemptions.filter(redeemed).values('coupon__kind')
        .annotate(uses=Count('id'), discount=_sum(F('discount_amount') + F('shipping_discount')))
    }
    kind_labels = dict(Coupon.KIND_CHOICES)
    by_kind_rows = [{'kind': kind_labels.get(k, k), 'uses': row['uses'], 'discount': row['discount']} for k, row in by_kind.items()]

    # ---- سری روزانه‌ی اخیر (فقط مصرف‌های نهایی) ----
    daily_since = now - timedelta(days=DAILY_DAYS - 1)
    daily = list(
        CouponRedemption.objects.filter(redeemed, reserved_at__gte=daily_since.replace(hour=0, minute=0, second=0, microsecond=0))
        .annotate(day=TruncDate('reserved_at')).values('day')
        .annotate(uses=Count('id'), discount=_sum(F('discount_amount') + F('shipping_discount'))).order_by('-day')
    )
    for row in daily:
        row['day_label'] = jdatetime.date.fromgregorian(date=row['day']).strftime('%Y/%m/%d')

    # ---- سفارش‌ها: تخفیف خودکار، کوپن و ارسال رایگان (یک کوئری) ----
    orders_row = orders.aggregate(
        orders=Count('id'),
        with_coupon=Count('id', filter=~Q(coupon_code='')),
        with_promotion=Count('id', filter=Q(promotion_discount__gt=0)),
        with_free_shipping=Count('id', filter=Q(shipping_discount__gt=0)),
        promotion_total=_sum('promotion_discount'),
        coupon_goods_discount=_sum('order_discount'),
        shipping_waived=_sum('shipping_discount'),
        revenue=_sum('total_price'),
    )
    order_count = orders_row['orders']
    orders_row['coupon_share'] = round(orders_row['with_coupon'] * 100 / order_count, 1) if order_count else 0
    orders_row['promotion_share'] = round(orders_row['with_promotion'] * 100 / order_count, 1) if order_count else 0
    # کرایه‌ی بخشیده‌شده توسط کوپن = مصرف‌های نهایی + رزروهای هنوز معتبر (سفارششان هم در جمع سفارش‌ها هست)
    coupon_waived = coupons_row['shipping_total'] + coupons_row['waiting_shipping']
    orders_row['shipping_waived_by_coupon'] = min(coupon_waived, orders_row['shipping_waived'])
    orders_row['shipping_waived_by_rule'] = orders_row['shipping_waived'] - orders_row['shipping_waived_by_coupon']

    # ---- تخفیف‌های خودکار: پرتخفیف‌ترین کالاها (کمپین روی سفارش snapshot نمی‌شود) ----
    line_discount = ExpressionWrapper(F('discount_amount') * F('quantity'), output_field=_MONEY)
    top_products = list(
        items.filter(discount_amount__gt=0).values('product_id', 'product__name')
        .annotate(units=Sum('quantity'), discount=Sum(line_discount), orders=Count('order_id', distinct=True))
        .order_by('-discount', '-units', 'product_id')[:TOP_N]
    )

    # ---- وضعیت فعلی ----
    inventory = Coupon.objects.aggregate(
        total=Count('id'),
        active=Count('id', filter=Q(is_active=True) & (Q(starts_at__isnull=True) | Q(starts_at__lte=now)) & (Q(ends_at__isnull=True) | Q(ends_at__gte=now))),
        claimable=Count('id', filter=Q(is_claimable=True, is_active=True)),
    )
    inventory['claims_in_period'] = claims.count()

    return {
        'days': days, 'since': since, 'now': now, 'coupons': coupons_row, 'orders': orders_row, 'inventory': inventory,
        'top_coupons': top_rows, 'top_by_discount': top_by_discount, 'by_kind': by_kind_rows, 'daily': daily,
        'top_products': top_products,
    }

