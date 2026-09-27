"""
ماشین‌حساب مرجوعی کالا (Phase 1 - Part B).

ماژول کاملاً خالص و بدون نوشتن در دیتابیس است (هیچ .save()/.create()ای این‌جا نیست)؛ نتیجه‌ی
هر تابع فقط از روی داده‌ی خواندنی محاسبه می‌شود. لایه‌ی سرویس (returns/services.py) این توابع
را در لحظه‌ی درست (گذار به REFUND_PENDING) صدا می‌زند و خودِ نتیجه را در دیتابیس اسنپ‌شات می‌کند
(returns/models.py:ReturnItem.refund_amount / ReturnRequest.shipping_refund_amount) - این ماژول
هیچ‌وقت مستقیم صدا زده نمی‌شود تا چیزی «زنده» محاسبه و به کاربر نشان داده شود.
"""

from decimal import Decimal

from django.db.models import Sum

from holoo.invoice import allocate_discount

from .models import ReturnItem, ReturnRequest


def get_order_item_allocated_unit_price(order_item):
    """
    فیِ هر واحد این OrderItem، پس از تسهیم عادلانه‌ی order.order_discount (مبلغ کد تخفیف سفارش)
    بین همه‌ی اقلام سفارش با allocate_discount (همان تابعی که holoo/invoice.py برای فاکتور هلو
    استفاده می‌کند - منبع واحد این محاسبه، نه پیاده‌سازی دوباره).

    order_item.price از قبل بعد از تخفیف خودکار محصول (Promotion) است؛ کوپن هرگز در سطح ردیف
    ذخیره نشده (فقط Order.order_discount به‌صورت یک عدد کلی) - این تابع دقیقاً همان سهمِ ازقلم‌افتاده
    را این‌جا (در لحظه‌ی محاسبه‌ی ریفاند) حساب می‌کند.

    ترتیب ردیف‌ها هنگام صدا زدن allocate_discount باید هر بار یکسان باشد (چون باقی‌مانده‌ی
    گردکردن به آخرین ردیف می‌رود)؛ این‌جا با order_by('pk') تضمین شده.

    اگر کمیتِ خودِ ردیف مقسوم‌علیه دقیق مبلغِ تخصیص‌یافته‌ی آن نباشد، باقی‌مانده (حداکثر
    quantity−1 ریال) به نفع مشتری صرف‌نظر نمی‌شود بلکه در محاسبه‌ی سرجمع می‌ماند - نگاه کنید
    توضیح داخل تابع.
    """
    order = order_item.order
    order_items = list(order.items.order_by('pk'))
    rows = [(oi.price, oi.quantity) for oi in order_items]
    parts = allocate_discount(rows, order.order_discount or 0)

    for oi, pieces in zip(order_items, parts):
        if oi.pk == order_item.pk:
            row_total = sum(unit_price * qty for unit_price, qty in pieces)
            # فیِ واحد صحیح: تقسیم صحیح؛ باقی‌مانده (در صورت وجود) به آخرین واحدهای همین ردیف می‌رود
            # تا سرجمعِ ردیف دقیقاً با row_total برابر بماند - همان روش allocate_discount خودش.
            return Decimal(row_total) // order_item.quantity

    raise ValueError('این OrderItem متعلق به سفارشِ دریافتی نیست.')


def calculate_item_refund_amount(return_item):
    """ مبلغ ریفاند یک ReturnItem بر مبنای approved_quantity (نه requested_quantity) """
    approved = return_item.approved_quantity or 0
    if approved <= 0:
        return Decimal('0')
    unit_price = get_order_item_allocated_unit_price(return_item.order_item)
    return unit_price * approved


def is_full_order_return(order):
    """
    آیا با احتساب approved_quantity ثبت‌شده‌ی مرجوعی‌های REFUND_PENDING/COMPLETED این سفارش،
    کل اقلام خریداری‌شده برگشت خورده‌اند؟ مرجوعی‌های هنوز-بازرسی‌نشده (PENDING/APPROVED/
    ITEM_RECEIVED بدون approved_quantity قطعی) عمداً حساب نمی‌شوند - این تابع فقط باید بعد از
    قطعی‌شدنِ approved_quantity مرجوعیِ جاری صدا زده شود (نگاه کنید mark_refund_pending).
    """
    total_purchased = sum(oi.quantity for oi in order.items.all())
    if total_purchased == 0:
        return False
    total_approved = ReturnItem.objects.filter(
        return_request__order=order,
        return_request__status__in=(ReturnRequest.STATUS_REFUND_PENDING, ReturnRequest.STATUS_COMPLETED),
    ).aggregate(total=Sum('approved_quantity'))['total'] or 0
    return total_approved >= total_purchased


def calculate_shipping_refund(order, *, exclude_return_request=None):
    """
    مبلغ کرایه‌ای که *در همین لحظه* باید مسترد شود؛ ۰ اگر سفارش هنوز کامل برنگشته یا کرایه‌اش
    قبلاً (در یک درخواست دیگر) مسترد شده - این چک ضدتکرار (Idempotency) دقیقاً همانی است که
    الزام شده: «هزینه ارسال دقیقاً یک‌بار برگشت داده شود».
    """
    if not is_full_order_return(order):
        return Decimal('0')

    already_refunded = ReturnRequest.objects.filter(order=order, shipping_refunded=True)
    if exclude_return_request is not None:
        already_refunded = already_refunded.exclude(pk=exclude_return_request.pk)
    if already_refunded.exists():
        return Decimal('0')

    return order.shipping_cost or Decimal('0')


def get_returnable_quantity(order_item):
    """
    ظرفیت باقیمانده‌ی قابل‌مرجوعِ این OrderItem (مرجع مشترک برای فرم‌ها/سرویس‌ها).

      - PENDING / APPROVED (پیش از بازرسی فیزیکی): بر مبنای requested_quantity رزرو محتاطانه
        می‌شود، چون تا بازرسی، تعداد نهایی معلوم نیست و نباید ظرفیتی بیش از حد آزاد بماند.
      - ITEM_RECEIVED / REFUND_PENDING / COMPLETED (پس از بازرسی): سوییچ به approved_quantity؛
        سهمِ ردشده‌ی کارشناس انبار فوراً به ظرفیت کاربر برمی‌گردد.
      - REJECTED: اصلاً شمارش نمی‌شود (خارج از ReturnRequest.ACTIVE_STATUSES). مدل فعلی هیچ
        وضعیت «لغوشده»ی جدایی برای ReturnRequest ندارد؛ تنها وضعیت غیرفعال، REJECTED است.
    """
    locked = 0
    active_items = ReturnItem.objects.filter(
        order_item=order_item, return_request__status__in=ReturnRequest.ACTIVE_STATUSES,
    ).select_related('return_request')
    pre_inspection = (ReturnRequest.STATUS_PENDING, ReturnRequest.STATUS_APPROVED)
    for item in active_items:
        if item.return_request.status in pre_inspection:
            locked += item.requested_quantity
        else:
            locked += item.approved_quantity or 0
    return max(0, order_item.quantity - locked)
