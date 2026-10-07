"""
پیامک‌های گفتگو (از «تنظیمات انواع پیام»؛ روشن/خاموش و متن آن‌جاست). این‌جا فقط «چه کسی و چه موقع» تصمیم گرفته می‌شود:

  - به کارشناسان (chat_notify_phones، یا ADMIN_NOTIFICATION_RECIPIENT): پیام تازه‌ی ناهمزمان. حداکثر یک پیامک برای هر گفتگو در
    cooldown (chat_admin_sms_cooldown_minutes).
  - به مشتری: فقط پاسخ **انسانی** کارشناس به گفتگوی ناهمزمان (T13/T14)، فقط وقتی مشتری واردشده است (شماره‌ی حسابش با OTP تأیید
    شده) یا شماره‌ی مهمان تأیید شده؛ مشتری در ۶۰ ثانیه‌ی اخیر در گفتگو نبوده؛ و cooldown گذشته. مهمانِ تأییدنشده هرگز پیامک نمی‌گیرد.
متن پیامک از _sms_text (حذف لینک/خط جدید) عبور می‌کند. هیچ خطایی این‌جا نباید مسیر ارسال پیام را بشکند.
"""
import logging
from datetime import timedelta

from django.conf import settings as django_settings
from django.utils import timezone

from products.chat_settings import parse_phone_list

from . import cache as chatcache
from .text import safe_inline

logger = logging.getLogger(__name__)


def admin_recipients(cfg):
    try:
        phones = parse_phone_list(cfg.chat_notify_phones)
    except ValueError:
        phones = []
    if not phones:
        fallback = getattr(django_settings, 'ADMIN_NOTIFICATION_RECIPIENT', None)
        phones = [fallback] if fallback else []
    return phones


def _cooldown_ok(kind, conversation_id, minutes, template_key):
    """
    cooldown: با Redis (cache.add)؛ اگر قطع بود، از DB: اگر در این بازه برای همین کلید پیامی ثبت شده، نه (محافظه‌کارانه و کلی).
    """
    seconds = max(1, int(minutes)) * 60
    acquired = chatcache.acquire(f'sms:{kind}:{conversation_id}', seconds)
    if acquired is not None:
        return bool(acquired)
    from notifications.models import Notification
    since = timezone.now() - timedelta(seconds=seconds)
    return not Notification.objects.filter(template_key=template_key, created_at__gte=since).exists()


def notify_operators_of_message(conversation, cfg):
    """ پیام تازه‌ی ناهمزمان برای کارشناسان؛ ← تعداد پیامک‌های صف‌شده """
    from notifications.service import notify

    try:
        if not _cooldown_ok('admin', conversation.pk, cfg.chat_admin_sms_cooldown_minutes, 'chat_offline_message_admin'):
            return 0
        sent = 0
        for phone in admin_recipients(cfg):
            if notify(phone, 'chat_offline_message_admin', name=safe_inline(conversation.display_name, 40) or 'مهمان'):
                sent += 1
        return sent
    except Exception:  # noqa: BLE001
        logger.exception('ارسال پیامک گفتگو به کارشناسان ناموفق بود.')
        return 0


def customer_phone(conversation):
    """ شماره‌ای که مجاز است به آن پیامک پاسخ برویم، یا '' (مهمانِ تأییدنشده → هرگز) """
    if conversation.user_id:
        return conversation.user.phone_number
    if conversation.guest_phone_verified and conversation.guest_phone:
        return conversation.guest_phone
    return ''


def notify_customer_of_reply(conversation, cfg):
    from notifications.service import notify

    try:
        phone = customer_phone(conversation)
        if not phone:
            return 0
        if chatcache.customer_seen_recently(conversation.pk):
            return 0                                   # همین حالا در گفتگوست و پاسخ را می‌بیند (None = Redis قطع ← ادامه)
        if not _cooldown_ok('cust', conversation.pk, cfg.chat_customer_sms_cooldown_minutes, 'chat_reply_customer'):
            return 0
        name = (conversation.user.first_name if conversation.user_id else conversation.guest_name) or 'مشتری'
        return 1 if notify(phone, 'chat_reply_customer', name=safe_inline(name, 40) or 'مشتری') else 0
    except Exception:  # noqa: BLE001
        logger.exception('ارسال پیامک پاسخ گفتگو به مشتری ناموفق بود.')
        return 0
