"""
اتصال loyalty به رویدادهای اپ‌های دیگر (Loyalty Phase 2B/2C/2D).

فقط در این اپ (نه در payments/orders/returns) - چون هیچ‌کدام از آن‌ها نباید loyalty را import
کند. دقیقاً هم‌الگوی notifications/receivers.py و payments/receivers.py: اپ گیرنده سیگنالِ اپ
فرستنده را import می‌کند، نه برعکس؛ جهت وابستگی یک‌طرفه (payments/orders/returns → loyalty)
هرگز برقرار نمی‌شود چون هیچ‌کدام از آن‌ها چیزی از loyalty نمی‌دانند.
"""

import logging

from django.dispatch import receiver

from orders.signals import order_canceled
from payments.signals import payment_succeeded
from returns.signals import return_refund_completed

from . import cancellation, earning
from .returns import reverse_from_return

logger = logging.getLogger(__name__)


@receiver(payment_succeeded, dispatch_uid='loyalty_earn_on_payment_succeeded')
def on_payment_succeeded(sender, order, transaction, **kwargs):
    """
    عمداً بدون try/except اینجا - نه فراموشی. payment_succeeded با send_robust شلیک می‌شود
    (payments/views.py:PaymentCallbackView._on_payment_succeeded) که خودش استثنای هر شنونده
    را می‌گیرد و لاگ می‌کند (logger.exception) بدون اینکه شنونده‌های دیگر یا مسیر اصلی پرداخت
    را بشکند - دقیقاً همان مرزی که notifications/receivers.py:on_payment_succeeded هم به آن
    تکیه می‌کند. یک try/except تکراری اینجا فقط همان لاگ را دوباره می‌کرد بدون فایده‌ی اضافه.

    (برخلاف on_order_canceled پایین که try/except *صریح* دارد - دلیلش آن‌جا مستند شده.)
    """
    earning.earn_from_payment(order, transaction)


@receiver(order_canceled, dispatch_uid='loyalty_reverse_on_order_canceled')
def on_order_canceled(sender, order, **kwargs):
    """
    برخلاف on_payment_succeeded بالا، اینجا try/except صریح *لازم* است، نه صرفاً محافظه‌کاری
    اضافی. دلیل: orders/models.py:Order.save() نتیجه‌ی order_canceled.send_robust(...) را
    می‌خواند و دور می‌ریزد (transaction.on_commit(lambda: order_canceled.send_robust(...)) -
    بدون هیچ حلقه‌ای که خروجی را بررسی/لاگ کند، برخلاف payments/views.py:_on_payment_succeeded
    برای payment_succeeded). یعنی send_robust باز هم استثنای این شنونده را از بقیه‌ی
    شنونده‌ها/از مسیر اصلی لغو سفارش جدا می‌کند (Isolation تضمین‌شده در هر حالت)، اما اگر خودمان
    اینجا لاگ نکنیم، خطا (مثلاً InsufficientPointsError از کسری موجودی - نگاه کنید
    loyalty/cancellation.py) کاملاً بی‌صدا گم می‌شود. این دقیقاً همان دلیلی است که
    payments/receivers.py:on_order_canceled هم try/except صریح خودش را دارد.
    """
    try:
        cancellation.reverse_from_cancellation(order)
    except Exception:
        logger.exception(
            "بازگشت امتیاز به دلیل لغو سفارش #%s ناموفق بود؛ بررسی دستی لازم است.", order.id,
        )


@receiver(return_refund_completed, dispatch_uid='loyalty_reverse_on_return_refund_completed')
def on_return_refund_completed(sender, return_request, **kwargs):
    """
    همان دلیل on_order_canceled بالا: returns/services.py:complete_refund هم نتیجه‌ی
    return_refund_completed.send_robust(...) را می‌خواند و دور می‌ریزد
    (db_transaction.on_commit(lambda: return_refund_completed.send_robust(...))، بدون حلقه‌ی
    لاگ‌کننده) - پس try/except صریح اینجا هم لازم است، نه اختیاری.
    """
    try:
        reverse_from_return(return_request)
    except Exception:
        logger.exception(
            "بازگشت امتیاز به دلیل مرجوعی #%s (سفارش #%s) ناموفق بود؛ بررسی دستی لازم است.",
            return_request.id, return_request.order_id,
        )
