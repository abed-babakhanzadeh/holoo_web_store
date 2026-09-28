"""
موتور بازگشت نسبی امتیاز از مرجوعی کالا (Loyalty Phase 2D).

بر خلاف بازگشت لغو سفارش (loyalty/cancellation.py که همیشه *کل* مانده را برمی‌گرداند)،
بازگشت مرجوعی فقط به *نسبت* سهم ریالی همین درخواست مرجوعی از کل اقلام سفارش برمی‌گردد - چون
مرجوعی جزئی یعنی فقط بخشی از خرید نامعتبر شده، نه کل آن (نگاه کنید گزارش تحلیل اثرات فاز ۲،
بخش ز). فقط در مرجوعیِ *کامل* (is_full_order_return) این نسبت کنار گذاشته می‌شود و دقیقاً کل
باقیمانده برمی‌گردد - تا رند کردن به پایین (floor) چیزی از امتیاز مشتری در جیب فروشگاه نگذارد.

این ماژول از returns.models/returns.refund_calculator فقط *می‌خواند* (هرگز چیزی در آن‌ها
نمی‌نویسد) - جهت وابستگی تک‌طرفه‌ی loyalty → returns، دقیقاً مصوبه‌ی گزارش تحلیل اثرات فاز ۲
(بخش ک: returns هرگز از loyalty وارد نمی‌شود، فقط برعکس).
"""

from decimal import Decimal

from django.db.models import Sum

from returns.models import ReturnRequest
from returns.refund_calculator import is_full_order_return

from . import services
from .models import LoyaltyTransaction


def _find_earn_transaction(order):
    return LoyaltyTransaction.objects.filter(
        source_type='order', source_id=order.id, transaction_type=LoyaltyTransaction.EARN_ORDER,
    ).first()


def _already_reversed_amount(order):
    """
    مجموع (مثبت) همه‌ی بازگشت‌های قبلیِ *مرتبط با همین سفارش* - هم بازگشت لغو
    (loyalty/cancellation.py، source_type='order') و هم بازگشت‌های مرجوعیِ قبلیِ همین سفارش
    (source_type='return_request'، برای هر ReturnRequest متعلق به این Order). سقف نهایی E باید
    مجموع هر دو نوع را در نظر بگیرد - وگرنه لغو و مرجوعی هرکدام جدا فکر می‌کردند سهمیه‌ی کامل
    خودشان را دارند.
    """
    order_scoped = LoyaltyTransaction.objects.filter(
        source_type='order', source_id=order.id, transaction_type=LoyaltyTransaction.REVERSE,
    ).aggregate(total=Sum('amount'))['total'] or 0

    return_request_ids = list(ReturnRequest.objects.filter(order=order).values_list('id', flat=True))
    return_scoped = 0
    if return_request_ids:
        return_scoped = LoyaltyTransaction.objects.filter(
            source_type='return_request', source_id__in=return_request_ids,
            transaction_type=LoyaltyTransaction.REVERSE,
        ).aggregate(total=Sum('amount'))['total'] or 0

    return -(order_scoped + return_scoped)   # amount روی رکوردهای REVERSE منفی ذخیره می‌شود


def calculate_reverse_amount(return_request):
    """
    مقدار امتیازی که باید بابت این ReturnRequest برگردد (int، همیشه >= 0).

    E = amount رکورد EARN_ORDER همین سفارش (اگر چنین رکوردی نباشد: 0 - سفارش هرگز امتیاز
        نگرفته بود، پس چیزی برای برگرداندن نیست).
    already_reversed = _already_reversed_amount(order) (لغو + مرجوعی‌های قبلی).
    remaining = E - already_reversed (سقف قطعی این و هر بازگشت بعدی).

    مرجوعی کامل: remaining عیناً برمی‌گردد (بدون فرمول نسبی) - هم چون منطقاً باید کل مانده
    برگردد، هم برای جلوگیری از هدررفت ناشی از رند کردن به پایین در فرمول نسبی.

    مرجوعی جزئی: raw = floor(E × R / B)، سپس min(raw, remaining) - سقفِ E هرگز رد نمی‌شود حتی
    اگر جمع رندهای چند مرجوعی متوالی کمی رو به پایین منحرف شود.
    """
    order = return_request.order
    earn_txn = _find_earn_transaction(order)
    if earn_txn is None:
        return 0

    e_amount = earn_txn.amount
    remaining = e_amount - _already_reversed_amount(order)
    if remaining <= 0:
        return 0

    if is_full_order_return(order):
        return remaining

    b_amount = order.items_total
    if not b_amount:
        return 0

    r_amount = return_request.total_refund_amount - (return_request.shipping_refund_amount or Decimal('0'))
    if r_amount <= 0:
        return 0

    raw_reverse = int((Decimal(e_amount) * r_amount) // b_amount)
    return min(raw_reverse, remaining)


def reverse_from_return(return_request):
    """
    نقطه‌ی ورود واحد از loyalty/receivers.py::on_return_refund_completed.

    Silent No-Op (خروجی None، بدون استثنا) اگر مقدار محاسبه‌شده صفر/منفی باشد (سفارش هرگز
    EARN_ORDER نگرفته، یا این مرجوعی هیچ سهمی از امتیازِ باقیمانده ندارد). کسری موجودی
    (InsufficientPointsError از services.debit_points) اینجا عمداً بلعیده نمی‌شود؛ به
    فراخوان‌کننده (loyalty/receivers.py::on_return_refund_completed) صعود می‌کند تا آن‌جا لاگ و
    ایزوله شود - دقیقاً هم‌الگوی loyalty/cancellation.py::reverse_from_cancellation.
    """
    to_reverse = calculate_reverse_amount(return_request)
    if to_reverse <= 0:
        return None

    order = return_request.order
    return services.debit_points(
        order.user, to_reverse, LoyaltyTransaction.REVERSE,
        f'برگشت نسبی امتیاز به دلیل مرجوعی #{return_request.id} (سفارش #{order.id})',
        source_type='return_request', source_id=return_request.id,
        idempotency_key=f'loyalty-reverse-return-{return_request.id}',
    )
