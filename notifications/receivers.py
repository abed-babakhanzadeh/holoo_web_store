"""
تنها نقطه‌ی اتصال اطلاع‌رسانی به رویدادهای پروژه.

payments و accounts دیگر نمی‌دانند پس از پرداخت یا تکمیل پروفایل پیامی می‌رود یا نه.
برای اضافه/کم کردن یک اطلاع‌رسانی، فقط همین فایل و templates_registry.py دست می‌خورند.
"""

import logging
import re

from django.dispatch import receiver
from django.utils import timezone

from accounts.signals import profile_completed, user_approved, user_registered, user_resubmitted_for_review
from orders.signals import order_placed
from payments.signals import payment_succeeded
from products.signals import contact_message_received, product_back_in_stock

from .service import notify, notify_admin

logger = logging.getLogger(__name__)


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


@receiver(user_registered, dispatch_uid='notify_user_registered')
def on_user_registered(sender, user, **kwargs):
    notify_admin('user_registered_admin', phone=user.phone_number)


@receiver(profile_completed, dispatch_uid='notify_profile_completed')
def on_profile_completed(sender, user, **kwargs):
    notify_admin(
        'profile_completed_admin',
        full_name=f"{user.first_name or ''} {user.last_name or ''}".strip(),
        phone=user.phone_number,
    )


@receiver(user_resubmitted_for_review, dispatch_uid='notify_user_resubmitted_for_review')
def on_user_resubmitted_for_review(sender, user, **kwargs):
    # قالب اختصاصی، نه profile_completed_admin: این پرونده قبلاً یک‌بار رد شده، مدیر باید
    # بداند با تکمیل اولیه‌ی یک مشتری تازه طرف نیست، بلکه با اصلاح یک پرونده‌ی ردشده
    notify_admin(
        'user_resubmitted_admin',
        full_name=f"{user.first_name or ''} {user.last_name or ''}".strip(),
        phone=user.phone_number,
    )


@receiver(user_approved, dispatch_uid='notify_user_approved')
def on_user_approved(sender, user, **kwargs):
    notify(user.phone_number, 'account_approved_customer', name=user.first_name or '')


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
        if alert.channel in (StockAlert.CHANNEL_EMAIL, StockAlert.CHANNEL_BOTH):
            notify(
                alert.email or alert.user.email, 'back_in_stock_email',
                backend='notifications.backends.email.EmailBackend',
                name=alert.user.first_name or '', product_name=product.name,
            )
        if alert.channel in (StockAlert.CHANNEL_SMS, StockAlert.CHANNEL_BOTH):
            notify(alert.user.phone_number, 'back_in_stock_sms', product_name=product.name)


def _sms_text(value, limit):
    """ متن کاربر را برای پیامک امن می‌کند: بدون خط جدید/کاراکتر کنترلی، لینک جایگزین می‌شود (تا فرم تماس کانالی
    برای رساندن لینک فیشینگ به پیامک مدیر نباشد) و طول محدود می‌شود """
    value = re.sub(r'(?:https?://|www\.)\S+', '[لینک]', value or '')
    value = re.sub(r'\s+', ' ', value).strip()
    return value if len(value) <= limit else value[:limit - 1].rstrip() + '…'


@receiver(contact_message_received, dispatch_uid='notify_contact_message_received')
def on_contact_message_received(sender, message, **kwargs):
    from products.models import SiteSettings

    context = {'name': _sms_text(message.name, 40), 'subject': _sms_text(message.subject, 60)}
    # گیرنده‌ها اول از مشخصات فروشگاه (حداکثر دو شماره‌ی مدیر، بدون تکرار)؛ اگر هیچ‌کدام پر نبود همان گیرنده‌ی
    # پیش‌فرض اعلان مدیر در تنظیمات سرور (notify_admin). ارسال به هر شماره مستقل است: خطا در یکی نباید
    # شماره‌ی دیگر را بی‌پیام بگذارد (notify هرگز استثنا نمی‌اندازد، ولی محافظ اضافه هم ضرری ندارد).
    recipients = SiteSettings.cached().store_admin_sms_recipients
    if not recipients:
        notify_admin('contact_message_admin', **context)
        return
    for recipient in recipients:
        try:
            notify(recipient, 'contact_message_admin', **context)
        except Exception:
            logger.exception('ارسال اعلان تماس با ما به %s ناموفق بود.', recipient)
