"""
موتور بازگشت امتیاز از لغو سفارش (Loyalty Phase 2C).

نگاه کنید loyalty/earning.py برای اصل «اسنپ‌شات به‌جای بازمحاسبه‌ی زنده» و مصوبه‌ی جداسازی
Loyalty Tier از Loyalty Points Ledger - همان اصول اینجا هم برقرارند.

بازگشتِ لغو همیشه *کل چیزی که هنوز برنگشته* را می‌گرداند (نه یک نسبت - بر خلاف بازگشت
مرجوعیِ جزئی که موضوع فاز ۲D است)، چون لغو سفارش یعنی «این خرید اصلاً معتبر نبود»، نه «بخشی
از آن برگردانده شد». مبنای این مقدار همیشه از خودِ لجر خوانده می‌شود (جمع رکوردهای واقعی)،
نه از فرمول زنده‌ی loyalty/earning.py:calculate_order_earn_points - چون آن فرمول با تغییر
بعدی SiteSettings.loyalty_* می‌تواند عدد متفاوتی بدهد؛ رکورد EARN_ORDER همان اسنپ‌شات قطعی
لحظه‌ی کسب است.

اصلاح ممیزی پیش‌کامیت فاز ۲ (bullet ۳): _already_reversed_amount باید دقیقاً هم‌شکل
loyalty/returns.py باشد - هم بازگشت‌های ناشی از لغو (source_type='order') و هم بازگشت‌های
ناشی از هر مرجوعیِ قبلیِ همین سفارش (source_type='return_request') را جمع بزند. پیش از این
اصلاح، این تابع فقط منبع اول را می‌دید - یعنی اگر سفارشی ابتدا بخشی از امتیازش را با یک
مرجوعی از دست داده بود و *بعد* لغو می‌شد، لغو کل E را دوباره برمی‌گرداند و جمع بازگشت‌ها را از
E عبور می‌داد. حالا هر دو منبع با هم دیده می‌شوند، دقیقاً مثل returns.py.
"""

from django.db.models import Sum

from returns.models import ReturnRequest

from . import services
from .models import LoyaltyTransaction


def _find_earn_transaction(order):
    return LoyaltyTransaction.objects.filter(
        source_type='order', source_id=order.id, transaction_type=LoyaltyTransaction.EARN_ORDER,
    ).first()


def _already_reversed_amount(order):
    """
    مجموع (مثبت) همه‌ی بازگشت‌های قبلیِ *مرتبط با همین سفارش* - هم بازگشت‌های ناشی از لغو
    (source_type='order') و هم بازگشت‌های ناشی از هر مرجوعیِ قبلیِ همین سفارش
    (source_type='return_request'، برای هر ReturnRequest متعلق به این Order). سقف نهایی E باید
    مجموع هر دو نوع را در نظر بگیرد - وگرنه لغو و مرجوعی هرکدام جدا فکر می‌کردند سهمیه‌ی کامل
    خودشان را دارند (دقیقاً هم‌شکل loyalty/returns.py::_already_reversed_amount).
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


def reverse_from_cancellation(order):
    """
    نقطه‌ی ورود واحد از loyalty/receivers.py::on_order_canceled.

    Silent No-Op (خروجی None، بدون استثنا) اگر:
      - هیچ EARN_ORDER ای برای این سفارش ثبت نشده باشد (سفارش هرگز پرداخت موفق نداشته، کاربر
        مهمان/حذف‌شده بوده، یا پیش از مرز فعال‌سازی ثبت شده بود - نگاه کنید loyalty/earning.py).
      - قبلاً کاملاً برگشته باشد (remaining<=0) - این دقیقاً همان چیزی است که شلیک مجدد سیگنال
        order_canceled را idempotent می‌کند، حتی پیش از رسیدن به idempotency_key خودِ debit_points.

    کسری موجودی (InsufficientPointsError از services.debit_points) اینجا عمداً بلعیده نمی‌شود؛
    به فراخوان‌کننده (loyalty/receivers.py::on_order_canceled) صعود می‌کند تا آن‌جا لاگ و
    ایزوله شود - نگاه کنید سربرگ آن تابع برای دلیل دقیق.
    """
    earn_txn = _find_earn_transaction(order)
    if earn_txn is None:
        return None

    remaining = max(0, earn_txn.amount - _already_reversed_amount(order))
    if remaining <= 0:
        return None

    return services.debit_points(
        order.user, remaining, LoyaltyTransaction.REVERSE,
        f'برگشت امتیاز به دلیل لغو سفارش #{order.id}',
        source_type='order', source_id=order.id,
        idempotency_key=f'loyalty-reverse-cancel-order-{order.id}',
    )
