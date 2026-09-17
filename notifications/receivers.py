"""
تنها نقطه‌ی اتصال اطلاع‌رسانی به رویدادهای پروژه.

payments و accounts دیگر نمی‌دانند پس از پرداخت یا تکمیل پروفایل پیامی می‌رود یا نه.
برای اضافه/کم کردن یک اطلاع‌رسانی، فقط همین فایل و templates_registry.py دست می‌خورند.
"""

from django.dispatch import receiver
from django.utils import timezone

from accounts.signals import profile_completed
from orders.signals import order_placed
from payments.signals import payment_succeeded
from products.signals import product_back_in_stock

from .service import notify, notify_admin


@receiver(order_placed, dispatch_uid='notify_order_placed')
def on_order_placed(sender, order, **kwargs):
    notify(
        order.user.phone_number, 'order_placed_customer',
        name=order.user.first_name or '', order_id=order.id,
    )


@receiver(payment_succeeded, dispatch_uid='notify_payment_succeeded')
def on_payment_succeeded(sender, order, transaction, **kwargs):
    amount = f"{order.total_price:,.0f}"
    notify(
        order.user.phone_number, 'payment_succeeded_customer',
        name=order.user.first_name or '', amount=amount, ref_id=transaction.ref_id,
    )
    notify_admin(
        'payment_succeeded_admin',
        order_id=order.id, amount=amount, phone=order.user.phone_number,
    )


@receiver(profile_completed, dispatch_uid='notify_profile_completed')
def on_profile_completed(sender, user, **kwargs):
    notify_admin(
        'profile_completed_admin',
        full_name=f"{user.first_name or ''} {user.last_name or ''}".strip(),
        phone=user.phone_number,
    )


@receiver(product_back_in_stock, dispatch_uid='notify_product_back_in_stock')
def on_product_back_in_stock(sender, product, **kwargs):
    from products.models import StockAlert

    alerts = list(
        StockAlert.objects.filter(product=product, status=StockAlert.STATUS_PENDING).select_related('user')
    )
    if not alerts:
        return

    # وضعیت را قبل از ارسال واقعی flip می‌کنیم (نه بعدش): notify() خودش صف/تلاش‌مجدد ارسال
    # را مدیریت می‌کند، پس همین‌جا «صف شد» به معنای انجام‌شده است — دقیقاً همان قراردادی که
    # بقیه‌ی پروژه هم دارد (مثل holoo_sync_alert_sent بلافاصله بعد از notify_admin).
    StockAlert.objects.filter(id__in=[a.id for a in alerts]).update(
        status=StockAlert.STATUS_NOTIFIED, notified_at=timezone.now(),
    )

    for alert in alerts:
        if alert.channel == StockAlert.CHANNEL_EMAIL:
            notify(
                alert.email or alert.user.email, 'back_in_stock_email',
                backend='notifications.backends.email.EmailBackend',
                name=alert.user.first_name or '', product_name=product.name,
            )
        else:
            notify(alert.user.phone_number, 'back_in_stock_sms', product_name=product.name)
