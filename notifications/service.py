"""
تنها API عمومی اطلاع‌رسانی.

    from notifications.service import notify
    notify(order.user.phone_number, 'payment_succeeded_customer', name=..., amount=..., ref_id=...)
    notify_admin('holoo_sync_stalled_admin', order_id=..., days=..., what=...)

فراخوان‌کننده فقط «مقصد + نوع پیام + داده» می‌دهد. اینکه پیام با پیامک برود یا ایمیل، با
کدام سرویس، با چند بار تلاش مجدد، و متنش دقیقاً چه باشد — هیچ‌کدام به فراخوان‌کننده مربوط نیست.
"""

import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.module_loading import import_string

from .backends.base import NotificationBackendError
from .models import Notification
from .templates_registry import render_message

logger = logging.getLogger(__name__)

DEFAULT_BACKEND = 'notifications.backends.console.ConsoleBackend'


def get_backend():
    """
    موتور ارسال فعال. منبع اصلی، فیلد «سرویس ارسال پیامک/اطلاع‌رسانی» در تنظیمات سایت است
    (کمبوی ادمین؛ بدون نیاز به دیپلوی مجدد قابل تغییر است). settings.NOTIFICATION_BACKEND
    فقط یک محافظ عقب‌افتاده است، برای وقتی ردیف تنظیمات سایت هنوز مقداردهی نشده.
    """
    from products.models import SiteSettings
    path = SiteSettings.cached().notification_backend or getattr(settings, 'NOTIFICATION_BACKEND', DEFAULT_BACKEND)
    return import_string(path)()


def notify(to, template_key, backend=None, **context):
    """
    یک پیام را ثبت و برای ارسال زمان‌بندی می‌کند و رکورد Notification را برمی‌گرداند.

    ارسال همیشه پس‌زمینه‌ای است (تسک سلری) و همیشه پس از commit تراکنش جاری شلیک می‌شود،
    تا هیچ‌وقت پیامی برای عملیاتی که در نهایت rollback شده ارسال نشود.
    این تابع هرگز استثنا به بالا پرتاب نمی‌کند: شکست اطلاع‌رسانی نباید مسیر اصلی کاربر
    (ثبت سفارش، پرداخت، ورود) را بشکند.

    backend: مسیر نقطه‌دار یک بک‌اند مشخص (مثلاً برای وقتی فراخوان‌کننده صریحاً کانال را
    انتخاب می‌کند، نه سرویس فعال سراسری سایت — مثل انتخاب ایمیل توسط خود کاربر برای اطلاع
    موجودی، جایی که سرویس سراسری ممکن است پیامک باشد). خالی/None یعنی از get_backend()
    (سرویس فعال تنظیمات سایت) استفاده شود، رفتار پیش‌فرض و بدون تغییر برای همه‌ی فراخوان‌های قدیمی.
    """
    if not to:
        logger.warning("پیام «%s» مقصدی ندارد؛ ارسال نشد.", template_key)
        return None

    try:
        text = render_message(template_key, context)
    except KeyError:
        logger.exception("ساخت متن پیام «%s» ناموفق بود.", template_key)
        return None

    try:
        notification = Notification.objects.create(
            recipient=str(to), template_key=template_key, text=text, context=context,
            backend_override=backend or '',
        )
    except Exception:
        logger.exception("ثبت پیام «%s» برای %s در دیتابیس ناموفق بود.", template_key, to)
        return None

    def _dispatch():
        from .tasks import deliver_notification
        try:
            deliver_notification.delay(notification.id)
        except Exception:
            # صف در دسترس نیست (مثلاً Redis پایین)؛ پیام در حالت pending می‌ماند و تسک
            # دوره‌ای retry_pending_notifications بعداً برش می‌دارد. هیچ پیامی گم نمی‌شود.
            logger.exception("شلیک تسک ارسال پیام %s ناموفق بود؛ در صف بازبینی می‌ماند.", notification.id)

    transaction.on_commit(_dispatch)
    return notification


def notify_admin(template_key, **context):
    """ اطلاع‌رسانی به مدیر سایت؛ مقصد از تنظیمات خوانده می‌شود نه از فراخوان‌کننده """
    return notify(getattr(settings, 'ADMIN_NOTIFICATION_RECIPIENT', None), template_key, **context)


def deliver(notification):
    """
    ارسال واقعی یک پیام (توسط تسک صدا زده می‌شود).
    در صورت خطای قابل‌تلاش‌مجدد، NotificationBackendError را به بالا پرتاب می‌کند تا تسک retry کند.
    """
    backend = import_string(notification.backend_override)() if notification.backend_override else get_backend()
    backend_path = f"{type(backend).__module__}.{type(backend).__name__}"

    notification.attempts += 1
    notification.backend = backend_path

    try:
        message_id = backend.send_template(
            notification.recipient, notification.template_key, notification.context, notification.text,
        )
    except NotificationBackendError as e:
        notification.status = Notification.STATUS_FAILED
        notification.error = str(e)[:2000]
        notification.save(update_fields=['attempts', 'backend', 'status', 'error'])
        raise
    except Exception as e:
        # خطای پیش‌بینی‌نشده‌ی موتور؛ مثل خطای قابل‌تلاش‌مجدد با آن رفتار می‌کنیم
        notification.status = Notification.STATUS_FAILED
        notification.error = f"{type(e).__name__}: {e}"[:2000]
        notification.save(update_fields=['attempts', 'backend', 'status', 'error'])
        raise NotificationBackendError(str(e)) from e

    notification.status = Notification.STATUS_SENT
    notification.provider_message_id = (message_id or '')[:190]
    notification.error = ''
    notification.sent_at = timezone.now()
    notification.save(update_fields=['attempts', 'backend', 'status', 'provider_message_id', 'error', 'sent_at'])
    return notification
