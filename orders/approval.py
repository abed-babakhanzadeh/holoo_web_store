"""
تأیید دومرحله‌ای سفارش توسط مدیر.

هیچ سفارشی (نقدی/کیف‌پول/چکی، حتی با پرداخت آنلاین موفق) خودکار فاکتور قطعی هلو نمی‌گیرد و انبار را تغییر نمی‌دهد. سفارش
پس از ثبت (و پرداخت یا ثبت چکی) «در انتظار تأیید مدیر / در حال بررسی» می‌ماند و رزرو موجودی‌اش در سایت نگه داشته می‌شود.
با «تأیید سفارش» در پنل مدیریت، رویداد order_approved اعلام می‌شود و فقط آن‌وقت ثبت فاکتور در هلو شلیک می‌شود
(holoo/receivers.py). اپ orders نمی‌داند چه کسی به این رویداد گوش می‌دهد.
"""

from django.db import transaction
from django.utils import timezone

from products.stock import InsufficientStock

from .models import Order
from .signals import order_approved
from .stock_hooks import order_lines
from products import stock


class ApprovalError(Exception):
    """ سفارش در وضعیتی نیست که تأیید شود؛ متن فارسی برای نمایش به مدیر """


def approval_blocker(order):
    """ دلیل غیرقابل‌تأیید بودن (متن فارسی) یا None وقتی قابل‌تأیید است """
    if order.approved_at:
        return 'این سفارش قبلاً تأیید شده است.'
    if order.status == 'canceled':
        return 'سفارش لغو شده است.'
    if order.status == 'rejected_stock':
        return 'سفارش به‌دلیل نبود موجودی رد شده است.'
    if order.status != 'pending':
        return 'وضعیت سفارش قابل‌تأیید نیست.'
    if not (order.is_paid or order.settled_off_site):
        return 'سفارش آنلاین هنوز پرداخت نشده است.'
    if not order.items.exists():
        return 'سفارش هیچ ردیفی ندارد.'
    return None


def approve_order(order, by=None):
    """
    تأیید سفارش (اتمیک، فقط یک‌بار): قفل سفارش ← بررسی ← اطمینان از رزرو فعالِ بی‌مهلت ← ثبت approved_at ←
    پس از commit، رویداد order_approved. سفارش‌هایی که پیش از این قابلیت ثبت شده‌اند و رزرو ندارند همین‌جا رزرو می‌شوند؛
    اگر موجودی نبود، تأیید رد می‌شود (ApprovalError).
    """
    with transaction.atomic():
        locked = Order.objects.select_for_update().get(pk=order.pk)
        reason = approval_blocker(locked)
        if reason:
            raise ApprovalError(reason)
        try:
            stock.reserve_for_order(locked.id, order_lines(locked), expires_at=None)
            stock.confirm_hold(locked.id)
        except InsufficientStock as error:
            raise ApprovalError(f'موجودی کافی نیست ({error}).')
        locked.approved_at = timezone.now()
        locked.approved_by = by if getattr(by, 'pk', None) else None
        locked.save(update_fields=['approved_at', 'approved_by', 'updated_at'])
        transaction.on_commit(lambda: order_approved.send_robust(sender=Order, order=locked))
    order.approved_at, order.approved_by = locked.approved_at, locked.approved_by
    return locked
