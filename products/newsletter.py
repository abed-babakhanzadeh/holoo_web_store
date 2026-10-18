"""
خبرنامه‌ی پیامکی: عضویت، لغو عضویت با لینک امضاشده، پیام خوش‌آمد و ارسال گروهی.

حسابرسی ۱۸ اکتبر ۲۰۲۶: فرم فوتر شماره را فقط ذخیره می‌کرد و هیچ‌جا چیزی برای اعضا ارسال نمی‌شد و راهی برای لغو نبود؛ فرم
سایدبار وبلاگ هم فقط ظاهر بود (ایمیل، بدون بک‌اند). حالا:

  - هر پیامی که به عضو می‌رود لینک لغو عضویت دارد (قالب‌های newsletter_* در notifications/templates_registry.py)؛
  - لینک لغو یک امضای HMAC کوتاه از شناسه‌ی عضو است (بدون ذخیره‌ی توکن، بدون انقضا؛ پیامک کوتاه می‌ماند) و صفحه‌ی لغو اول تأیید
    می‌خواهد (GET فقط نشان می‌دهد، POST لغو می‌کند) تا پیش‌نمایش‌گرهای لینک پیامک کسی را ناخواسته لغو نکنند؛
  - ثبت‌نام با شماره‌ی دیگران ممکن است؛ برای همین فقط روی عضویتِ تازه/فعال‌شده‌ی دوباره پیام خوش‌آمد می‌رود، هر IP ساعتی ۱۰ ثبت دارد،
    و پیام خوش‌آمد خودش لینک لغو دارد.
"""

import hashlib
import hmac
import logging

from django.conf import settings
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from .models import NewsletterSubscriber, SiteSettings

logger = logging.getLogger(__name__)

SUBSCRIBE_LIMIT_PER_HOUR = 10
SUBSCRIBE_WINDOW = 3600
THROTTLE_KEY = 'newsletter:subscribe:ip:{ip}'


# ----------------------------------------------------------------------------- لینک لغو
def _signature(subscriber_id):
    digest = hmac.new(settings.SECRET_KEY.encode(), f'newsletter-unsub:{subscriber_id}'.encode(), hashlib.sha256).hexdigest()
    return digest[:16]


def make_token(subscriber):
    return f'{subscriber.pk}-{_signature(subscriber.pk)}'


def subscriber_from_token(token):
    """ عضوِ مربوط به توکن یا None (قالب اشتباه، امضای نادرست یا عضو ناموجود) """
    try:
        raw_id, signature = str(token).split('-', 1)
        subscriber_id = int(raw_id)
    except ValueError:
        return None
    if not hmac.compare_digest(signature, _signature(subscriber_id)):
        return None
    return NewsletterSubscriber.objects.filter(pk=subscriber_id).first()


def unsubscribe_url(request, subscriber):
    return request.build_absolute_uri(reverse('products:newsletter_unsubscribe', args=[make_token(subscriber)]))


# ----------------------------------------------------------------------------- عضویت/لغو
def subscribe_throttled(ip):
    key = THROTTLE_KEY.format(ip=ip)
    if cache.add(key, 1, SUBSCRIBE_WINDOW):
        return False
    try:
        return cache.incr(key) > SUBSCRIBE_LIMIT_PER_HOUR
    except ValueError:
        cache.set(key, 1, SUBSCRIBE_WINDOW)
        return False


def subscribe(phone, source=NewsletterSubscriber.SOURCE_FOOTER):
    """ (عضو، آیا «تازه عضو شد»): ثبت جدید یا فعال‌شدن دوباره‌ی عضو لغوکرده. عضو فعالِ موجود دست‌نخورده می‌ماند """
    subscriber, created = NewsletterSubscriber.objects.get_or_create(phone_number=phone, defaults={'source': source})
    if created:
        return subscriber, True
    if not subscriber.is_active:
        subscriber.is_active = True
        subscriber.unsubscribed_at = None
        subscriber.save(update_fields=['is_active', 'unsubscribed_at'])
        return subscriber, True
    return subscriber, False


def unsubscribe(subscriber):
    if subscriber.is_active:
        subscriber.is_active = False
        subscriber.unsubscribed_at = timezone.now()
        subscriber.save(update_fields=['is_active', 'unsubscribed_at'])
        return True
    return False


# ----------------------------------------------------------------------------- ارسال
def send_welcome(request, subscriber):
    from notifications.service import notify
    return notify(subscriber.phone_number, 'newsletter_welcome', store=SiteSettings.cached().store_name,
                  unsubscribe_url=unsubscribe_url(request, subscriber))


def send_campaign(request, text, subscribers):
    """ متن را با لینک لغوِ هر عضو برای اعضای *فعالِ* فهرست می‌فرستد؛ تعداد پیام‌های ثبت‌شده را برمی‌گرداند """
    from notifications.service import notify
    sent = 0
    for subscriber in subscribers.filter(is_active=True).iterator(chunk_size=500):
        if notify(subscriber.phone_number, 'newsletter_campaign', text=text,
                  unsubscribe_url=unsubscribe_url(request, subscriber)):
            sent += 1
    return sent
