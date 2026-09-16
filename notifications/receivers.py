"""
تنها نقطه‌ی اتصال اطلاع‌رسانی به رویدادهای پروژه.

payments و accounts دیگر نمی‌دانند پس از پرداخت یا تکمیل پروفایل پیامی می‌رود یا نه.
برای اضافه/کم کردن یک اطلاع‌رسانی، فقط همین فایل و templates_registry.py دست می‌خورند.
"""

from django.dispatch import receiver

from accounts.signals import profile_completed
from payments.signals import payment_succeeded

from .service import notify, notify_admin


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
