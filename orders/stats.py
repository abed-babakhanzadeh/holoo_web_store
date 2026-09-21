"""آمار سفارش‌های کاربر برای پیشخوان پنل کاربری (ثبت در رجیستری accounts.stats)."""

from datetime import timedelta

import jdatetime
from django.db.models import F
from django.utils import timezone

from accounts.stats import register
from reviews.purchases import register_provider as register_purchase_provider

from .models import Order, OrderItem


@register_purchase_provider
def purchase_info(user, product):
    """
    آیا کاربر این محصول را در سفارشی با پرداخت موفق خریده، و با چه رنگی.
    اپ reviews این تابع را از طریق رجیستری صدا می‌زند و دیگر خودش orders را import نمی‌کند.
    """
    item = (
        OrderItem.objects
        .filter(product=product, order__user=user, order__transactions__status='success')
        .select_related('color')
        # ردیف رنگ‌دار در اولویت است تا اگر کاربر هم بی‌رنگ و هم رنگ‌دار خریده،
        # رنگ واقعی خریدش زیر نظر نمایش داده شود
        .order_by(F('color_id').asc(nulls_last=True), '-order__created_at')
        .first()
    )
    return (item is not None), (item.color if item else None)


@register('orders_total')
def orders_total(user):
    return Order.objects.filter(user=user).count()


@register('orders_placed_count')
def orders_placed_count(user):
    """ تعداد سفارش‌های ثبت‌شده‌ی غیرلغو (مبنای شرط «فقط اولین خرید» کدهای تخفیف؛ پرداخت‌نشده هم شمرده می‌شود) """
    return Order.objects.filter(user=user).exclude(status='canceled').count()


@register('orders_pending')
def orders_pending(user):
    return Order.objects.filter(user=user, status__in=['pending', 'registered']).count()


@register('orders_paid_count')
def orders_paid_count(user):
    """ تعداد سفارش‌های واقعاً پرداخت‌شده؛ مبنای امتیاز و سطح وفاداری """
    return Order.objects.filter(user=user, transactions__status='success').distinct().count()


@register('orders_recent')
def orders_recent(user):
    return list(Order.objects.filter(user=user).order_by('-created_at')[:5])


def _jalali_md(date_obj):
    """ برچسب روز/ماه شمسی (مثلاً 04/28) برای محور نمودار """
    j = jdatetime.date.fromgregorian(date=date_obj)
    return f"{j.month:02d}/{j.day:02d}"


def _jalali_ym(date_obj):
    """ برچسب سال/ماه شمسی (مثلاً 1404/04) برای محور نمودار """
    j = jdatetime.date.fromgregorian(date=date_obj)
    return f"{j.year}/{j.month:02d}"


@register('orders_activity_chart')
def orders_activity_chart(user):
    """
    داده‌ی نمودار فعالیت خرید کاربر (تعداد سفارش) در سه بازه‌ی هفته/ماه/سال،
    کاملاً بر پایه‌ی سفارش‌های واقعی کاربر، بدون هیچ داده‌ی ساختگی.
    برچسب‌های محور شمسی هستند؛ گروه‌بندی داخلی بر پایه‌ی تاریخ میلادی ذخیره‌شده باقی می‌ماند.
    """
    now = timezone.localtime()
    dates = [timezone.localtime(dt).date() for dt in Order.objects.filter(user=user).values_list('created_at', flat=True)]

    # --- هفته: ۷ روز گذشته، به تفکیک روز ---
    week_labels, week_data = [], []
    for i in range(6, -1, -1):
        day = (now - timedelta(days=i)).date()
        week_labels.append(_jalali_md(day))
        week_data.append(sum(1 for d in dates if d == day))

    # --- ماه: ۳۰ روز گذشته، به تفکیک هفته (۵ بازه) ---
    month_labels, month_data = [], []
    for i in range(4, -1, -1):
        start = (now - timedelta(days=(i + 1) * 6 + i)).date()
        end = (now - timedelta(days=i * 7)).date()
        month_labels.append(f"{_jalali_md(start)} تا {_jalali_md(end)}")
        month_data.append(sum(1 for d in dates if start <= d <= end))

    # --- سال: ۱۲ ماه گذشته، به تفکیک ماه میلادی (چون تاریخ ذخیره‌شده میلادی است) ---
    year_labels, year_data = [], []
    for i in range(11, -1, -1):
        ref = (now - timedelta(days=i * 30)).date()
        year_labels.append(_jalali_ym(ref))
        year_data.append(sum(1 for d in dates if (d.year, d.month) == (ref.year, ref.month)))

    return {
        'week': {'labels': week_labels, 'data': week_data},
        'month': {'labels': month_labels, 'data': month_data},
        'year': {'labels': year_labels, 'data': year_data},
    }
