"""
چرخه‌ی رزرو موجودی سفارش (سمت orders). منطق اتمیکِ خودِ رزرو در products/stock.py است؛ اینجا فقط «کدام سفارش، چه
مهلتی، در چه رویدادی» تصمیم‌گیری می‌شود و به رویدادهای دامنه گوش داده می‌شود:

  ثبت سفارش (SubmitOrderView)  ← hold_order_stock: رزرو اتمیک همه‌ی اقلام، یا شکست و بازگشت کل تراکنش
  شروع پرداخت (PaymentStartView) ← ensure_order_hold: اگر مهلت گذشته دوباره رزرو می‌شود (یا موجودی تمام شده)
  پرداخت موفق (payment_succeeded) ← مهلت برداشته می‌شود و سفارش «در انتظار تأیید مدیر» می‌ماند؛ اگر در فاصله‌ی درگاه
                                    رزرو منقضی و کالا تمام شده بود: reject_for_stock
  لغو سفارش (order_canceled)    ← آزادسازی
  رد برای نبود موجودی (خطای ۲۸ هلو یا اینجا) ← وضعیت rejected_stock + هشدار به مدیر؛ بازگشت وجه فقط با تصمیم دستی مدیر

مهلت: سفارش آنلاینِ پرداخت‌نشده ۲۰ دقیقه (products.stock.RESERVATION_TTL)؛ سفارش چکی یا پرداخت‌شده بدون مهلت تا تصمیم مدیر.
"""

import logging

from django.db import transaction
from django.dispatch import receiver
from django.utils import timezone

from payments.signals import payment_succeeded
from products import stock

from .signals import order_canceled

logger = logging.getLogger(__name__)

InsufficientStock = stock.InsufficientStock


def order_lines(order):
    """ {product_id: تعداد کل} (ردیف‌های هم‌کالا با رنگ‌های مختلف جمع می‌شوند؛ کالای حذف‌شده نادیده گرفته می‌شود) """
    lines = {}
    for item in order.items.all():
        if item.product_id:
            lines[item.product_id] = lines.get(item.product_id, 0) + item.quantity
    return lines


def hold_expiry(order, now=None):
    """ سفارش آنلاینِ پرداخت‌نشده: now + ۲۰ دقیقه. چکی (روش تسویه) یا پرداخت‌شده: None (تا تصمیم مدیر) """
    if order.is_cheque or order.is_paid:
        return None
    return (now or timezone.now()) + stock.RESERVATION_TTL


def hold_order_stock(order, now=None, lines=None):
    """
    رزرو اتمیک اقلام سفارش؛ داخل transaction.atomic صدا بزنید. InsufficientStock را بالا می‌دهد.

    lines: {product_id: تعداد}؛ در ثبت سفارش *پیش از ساخت ردیف‌های سفارش* داده می‌شود. ترتیب مهم است: SQL Server هنگام
    درج ردیفِ فرزند (OrderItem/StockReservation) روی ردیف محصولِ والد قفل اشتراکی می‌گیرد؛ اگر چند تراکنشِ هم‌زمان اول
    این را بگیرند و بعد همگی بخواهند UPDATE شرطیِ رزرو را بزنند، deadlock (تبدیل قفل) رخ می‌دهد. پس قفل انحصاری
    ردیف محصول (UPDATE رزرو) باید قبل از هر درجِ فرزند گرفته شود.
    """
    now = now or timezone.now()
    stock.reserve_for_order(order.id, lines if lines is not None else order_lines(order),
                            expires_at=hold_expiry(order, now), now=now)


def ensure_order_hold(order):
    """ پیش از هر پرداخت: رزرو فعال و تازه‌شدهٔ مهلت (دوباره‌رزرو اگر منقضی شده). InsufficientStock اگر دیگر موجود نیست. """
    with transaction.atomic():
        hold_order_stock(order)


def reject_for_stock(order, detail=''):
    """
    سفارش به‌دلیل نبود موجودی رد می‌شود (rejected_stock): رزرو آزاد، مدیر مطلع. هیچ بازگشت وجهی خودکار نیست؛
    اگر پرداخت شده بود، بازگرداندن وجه (کیف‌پول/درگاه) با تصمیم دستی مدیر است. True اگر همین فراخوانی وضعیت را عوض کرد.
    """
    from .models import Order

    with transaction.atomic():
        locked = Order.objects.select_for_update().get(pk=order.pk)
        if locked.status in ('canceled', 'rejected_stock'):
            return False
        paid = locked.is_paid
        locked.status = 'rejected_stock'
        locked.save(update_fields=['status', 'updated_at'])
        stock.release_order(locked.id, 'rejected_stock')
    order.status = 'rejected_stock'

    logger.error("سفارش %s به‌دلیل نبود موجودی رد شد (پرداخت‌شده=%s): %s", order.id, paid, detail)
    from notifications.service import notify_admin
    notify_admin(
        'order_rejected_stock_admin', order_id=order.id,
        paid_note="مبلغ آن پرداخت شده است؛ بازگشت وجه با تصمیم دستی شما." if paid else "",
    )
    if order.user is not None:
        from notifications.service import notify
        notify(order.user.phone_number, 'order_rejected_stock_customer',
               name=order.user.first_name or '', order_id=order.id,
               refund_note="همکاران ما برای بازگشت مبلغ پرداختی با شما هماهنگ می‌کنند. " if paid else "")
    return True


@receiver(payment_succeeded, dispatch_uid='orders_stock_confirm_on_payment')
def confirm_hold_on_payment(sender, order, **kwargs):
    """
    پرداخت موفق: مهلت رزرو برداشته می‌شود (سفارش تا «تأیید مدیر» رزرو می‌ماند). اگر درگاه آن‌قدر طول کشیده که رزرو
    منقضی شده بود، دوباره (اتمیک) رزرو می‌شود؛ اگر کالا دیگر نبود پول گرفته‌شده ولی کالایی نیست: reject_for_stock.
    """
    from .models import Order
    if Order.objects.filter(pk=order.pk, status__in=('canceled', 'rejected_stock')).exists():
        return                                   # سفارش لغو/رد شده؛ رزرو تازه ساخته نمی‌شود
    try:
        with transaction.atomic():
            stock.reserve_for_order(order.id, order_lines(order), expires_at=None)
            stock.confirm_hold(order.id)
    except InsufficientStock as error:
        reject_for_stock(order, str(error))
    except Exception:
        logger.exception("تأیید رزرو موجودی پس از پرداخت سفارش %s ناموفق بود.", order.id)


@receiver(order_canceled, dispatch_uid='orders_stock_release_on_cancel')
def release_stock_on_cancel(sender, order, **kwargs):
    """ لغو سفارش: رزرو موجودی آزاد می‌شود """
    stock.release_order(order.id, 'order_canceled')

