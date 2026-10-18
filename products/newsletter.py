"""
خبرنامه‌ی پیامکی: عضویت، لغو عضویت (صفحه‌ی لغو با لینک امضاشده)، پیام خوش‌آمد و ارسال گروهی.

حسابرسی ۱۸ اکتبر ۲۰۲۶: فرم فوتر شماره را فقط ذخیره می‌کرد و هیچ‌جا چیزی برای اعضا ارسال نمی‌شد و راهی برای لغو نبود؛ فرم
سایدبار وبلاگ هم فقط ظاهر بود (ایمیل، بدون بک‌اند). حالا:

  - پیامک خبرنامه *لینک ندارد* و به «لغو11» ختم می‌شود (قالب‌های newsletter_* در notifications/templates_registry.py): خط پیامک
    ملی‌پیامک متن حاوی لینک را با کد ۱۴ و متن تبلیغاتیِ بدون «لغو11» پایانی را با کد ۱۵ رد می‌کند، و هر دو در نسخه‌ی اول بی‌صدا
    «ارسال‌شده» ثبت می‌شدند ولی هرگز نمی‌رسیدند (ممیزی ۱۸ اکتبر ۲۰۲۶؛ notifications/backends/melipayamak.py حالا کد خطا را می‌خواند)؛
  - صفحه‌ی لغو عضویت (لینک فوتر، یا لینک امضاشده‌ی HMAC کوتاه از شناسه‌ی عضو) اول تأیید می‌خواهد (GET فقط نشان می‌دهد، POST لغو
    می‌کند) تا پیش‌نمایش‌گرهای لینک کسی را ناخواسته لغو نکنند؛
  - ثبت‌نام با شماره‌ی دیگران ممکن است؛ برای همین فقط روی عضویتِ تازه/فعال‌شده‌ی دوباره پیام خوش‌آمد می‌رود، هر IP ساعتی ۱۰ ثبت دارد،
    و پیام خوش‌آمد هم به «لغو11» ختم می‌شود.
"""

import hashlib
import hmac
import logging
import re

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
LINK_PATTERN = re.compile(r'(https?://|www\.|\b[\w-]+\.(ir|com|org|net|info|me|io|co)\b)', re.IGNORECASE)
TRAILING_OPT_OUT = re.compile(r'(\s*لغو\s*[1۱]{2}\s*)+$')


def campaign_text_problem(text):
    """ پیام خطا اگر متن کمپین قابل‌ارسال نیست (لینک ← خط پیامک رد می‌کند، کد ۱۴) وگرنه None """
    if LINK_PATTERN.search(text):
        return 'متن پیامک نباید لینک یا آدرس سایت داشته باشد؛ خط پیامک آن را رد می‌کند و پیام نمی‌رسد.'
    return None


def clean_campaign_text(text):
    """ «لغو11»ِ دستیِ انتهای متن حذف می‌شود؛ قالب پیام خودش در پایان «لغو11» می‌گذارد (دو بار نیاید) """
    return TRAILING_OPT_OUT.sub('', text.strip()).strip()


def send_welcome(request, subscriber):
    from notifications.service import notify
    return notify(subscriber.phone_number, 'newsletter_welcome', store=SiteSettings.cached().store_name)


def send_campaign(request, text, subscribers):
    """ متن را با لینک لغوِ هر عضو برای اعضای *فعالِ* فهرست می‌فرستد؛ تعداد پیام‌های ثبت‌شده را برمی‌گرداند """
    from notifications.service import notify
    sent = 0
    for subscriber in subscribers.filter(is_active=True).iterator(chunk_size=500):
        if notify(subscriber.phone_number, 'newsletter_campaign', text=text):
            sent += 1
    return sent
