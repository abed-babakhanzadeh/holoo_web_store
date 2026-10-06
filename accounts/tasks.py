"""
یادآوری تکمیل پروفایل (پیامک) برای مشتری‌ای که وارد شده ولی پروفایلش را کامل نکرده.

روشن/خاموش بودن و متن پیام از پنل ادمین (اطلاع‌رسانی ← تنظیمات انواع پیام، «یادآوری تکمیل پروفایل (به مشتری)») کنترل
می‌شود؛ پیش‌فرضِ آن خاموش است. اینجا فقط «چه کسی و چه موقع» تصمیم گرفته می‌شود:

  - فقط کاربری که واردشده (last_login دارد) و در ۱۴ روز گذشته آمده؛ کاربرِ قدیمیِ ساکت را دنبال نمی‌کنیم و با روشن کردنِ
    تنظیم، سیل پیامک به همه‌ی کاربران قدیمی نمی‌رود.
  - اولین یادآوری ۲۴ ساعت بعد از آخرین ورود، دومی ۳ روز بعد از اولی، و بعد توقف (حداکثر ۲ پیام برای هر شماره).
  - فقط ساعت ۹ تا ۲۱ به وقت محلی (پیامک شبانه مزاحمت است)؛ تسک ساعتی اجرا می‌شود و در بقیه‌ی ساعت‌ها کاری نمی‌کند.
  - سقف ۲۰۰ پیام در هر اجرا برای پخش بار.
شمارنده‌ها از خودِ رکوردهای Notification خوانده می‌شود؛ جدول جدا یا فیلد جدید لازم نیست.
"""
import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)

REMINDER_KEY = 'profile_incomplete_reminder_customer'
FIRST_REMINDER_AFTER = timedelta(hours=24)
SECOND_REMINDER_AFTER = timedelta(days=3)
MAX_REMINDERS = 2
ACTIVE_WINDOW = timedelta(days=14)
SEND_FROM_HOUR, SEND_UNTIL_HOUR = 9, 21
MAX_PER_RUN = 200


def eligible_for_reminder(user, sent_count, last_sent_at, now):
    """ آیا این کاربر الان باید یادآوری بگیرد؟ (منطق خالص، برای تست آسان) """
    if not user.needs_profile_completion or not user.is_active or user.last_login is None:
        return False
    if user.last_login > now - FIRST_REMINDER_AFTER or user.last_login < now - ACTIVE_WINDOW:
        return False
    if sent_count == 0:
        return True
    return sent_count < MAX_REMINDERS and last_sent_at is not None and last_sent_at <= now - SECOND_REMINDER_AFTER


@shared_task
def send_profile_reminders(now=None):
    """ تسک ساعتی (config/celery.py)؛ خروجی: خلاصه‌ی متنی """
    from django.db.models import Count, Max
    from notifications.models import Notification, NotificationSetting, sync_notification_settings
    from notifications.service import notify

    from .models import CustomUser

    now = now or timezone.now()
    hour = timezone.localtime(now).hour
    if not (SEND_FROM_HOUR <= hour < SEND_UNTIL_HOUR):
        return 'outside sending hours'

    sync_notification_settings()
    if not NotificationSetting.objects.filter(template_key=REMINDER_KEY, is_enabled=True).exists():
        return 'disabled'

    candidates = list(
        CustomUser.objects.filter(is_active=True, is_staff=False, is_superuser=False,
                                  last_login__isnull=False, last_login__lte=now - FIRST_REMINDER_AFTER,
                                  last_login__gte=now - ACTIVE_WINDOW)
        .order_by('last_login')
    )
    candidates = [user for user in candidates if user.needs_profile_completion]
    history = {}
    phones = [user.phone_number for user in candidates]
    for start in range(0, len(phones), 500):      # SQL Server حداکثر ~۲۱۰۰ پارامتر در یک کوئری
        for row in (Notification.objects.filter(template_key=REMINDER_KEY, recipient__in=phones[start:start + 500])
                    .values('recipient').annotate(sent=Count('id'), last=Max('created_at'))):
            history[row['recipient']] = row

    sent = 0
    for user in candidates:
        row = history.get(user.phone_number)
        if not eligible_for_reminder(user, row['sent'] if row else 0, row['last'] if row else None, now):
            continue
        if notify(user.phone_number, REMINDER_KEY, name=user.first_name or 'مشتری'):
            sent += 1
        if sent >= MAX_PER_RUN:
            break
    return f'sent={sent}'
