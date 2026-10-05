import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from .models import Notification

logger = logging.getLogger(__name__)

MAX_DELIVERY_ATTEMPTS = 5
# پیام‌هایی که بیش از این مدت در حالت pending مانده‌اند (یعنی تسکشان اصلاً اجرا نشده،
# مثلاً چون موقع شلیک Redis پایین بوده) توسط تسک بازبینی دوباره به صف می‌روند
STUCK_PENDING_AFTER = timedelta(minutes=5)

# فاصله‌ی تلاش مجدد (ثانیه). پیام‌های سریع (کد ورود) نباید بعد از یک خطای لحظه‌ایِ سرویس یک دقیقه معطل بمانند؛ بقیه
# تصاعدی تا سقف یک ساعت
FAST_RETRY_TEMPLATES = frozenset({'otp'})
FAST_RETRY_COUNTDOWNS = (3, 6, 12, 30, 60)


def retry_countdown(template_key, retries):
    """ ثانیه‌ی انتظار پیش از تلاش بعدی؛ retries تعداد تلاش‌های مجدد قبلی (۰ برای اولین retry) """
    if template_key in FAST_RETRY_TEMPLATES:
        return FAST_RETRY_COUNTDOWNS[min(retries, len(FAST_RETRY_COUNTDOWNS) - 1)]
    return min(60 * (2 ** retries), 3600)


@shared_task(bind=True, max_retries=MAX_DELIVERY_ATTEMPTS)
def deliver_notification(self, notification_id):
    """ ارسال واقعی یک پیام، با تلاش مجدد تصاعدی در صورت خطای سرویس """
    from .service import deliver

    try:
        notification = Notification.objects.get(id=notification_id)
    except Notification.DoesNotExist:
        return "Notification not found."

    if notification.status == Notification.STATUS_SENT:
        return f"Already sent: {notification.provider_message_id}"

    try:
        deliver(notification)
    except Exception as e:
        if self.request.retries >= MAX_DELIVERY_ATTEMPTS:
            # از تلاش دست می‌کشیم، ولی رکورد با وضعیت failed و متن خطا باقی می‌ماند تا در
            # پنل ادمین دیده و در صورت لزوم دستی دوباره ارسال شود
            logger.error("پیام %s پس از %s تلاش ارسال نشد: %s", notification_id, self.request.retries, e)
            return "Gave up."
        countdown = retry_countdown(notification.template_key, self.request.retries)
        logger.warning("ارسال پیام %s ناموفق (%s)؛ تلاش مجدد در %s ثانیه.", notification_id, e, countdown)
        raise self.retry(exc=e, countdown=countdown)

    return f"Sent: {notification.provider_message_id}"


@shared_task
def retry_pending_notifications():
    """
    تور ایمنی: پیام‌هایی که در صف گیر کرده‌اند (تسکشان شلیک نشده یا Worker وسط کار مرده)
    را دوباره به صف می‌فرستد. idempotent است چون پیام ارسال‌شده بلافاصله برمی‌گردد.
    """
    cutoff = timezone.now() - STUCK_PENDING_AFTER
    stuck = list(
        Notification.objects
        .filter(status=Notification.STATUS_PENDING, created_at__lt=cutoff, attempts__lt=MAX_DELIVERY_ATTEMPTS)
        .values_list('id', flat=True)[:500]
    )
    for notification_id in stuck:
        deliver_notification.delay(notification_id)

    if stuck:
        logger.info("بازبینی اطلاع‌رسانی: %s پیام گیرکرده دوباره به صف رفت.", len(stuck))
    return f"requeued={len(stuck)}"
