"""
اشتراک‌گذاری محصول: دیپ‌لینک‌های پیام‌رسان‌ها + متادیتای Open Graph.

تنها منبع این منطق؛ در تمپلیت urlencode دستی/زنجیره‌ای انجام نمی‌شود (مستعد
double-encoding است، مخصوصاً برای واتس‌اپ که عنوان+آدرس هر دو داخل یک پارامتر
متنی واحد قرار می‌گیرند) — همه‌چیز اینجا با urllib.parse.quote یک‌بار و تمیز
ساخته می‌شود. توابع خالص و بدون وابستگی به Request هستند تا به‌سادگی تست‌پذیر
باشند؛ ساخت absolute_url/image_url (که نیاز به request دارند) بر عهده‌ی ویو است.
"""
from urllib.parse import quote

from django.template.defaultfilters import truncatechars
from django.utils.html import strip_tags

OG_DESCRIPTION_MAX_LENGTH = 150
OG_DESCRIPTION_FALLBACK = 'فروشگاه اینترنتی هلو'


def _q(text):
    """ urlencode امن برای querystring؛ فاصله را %20 می‌کند (نه +) و کاراکترهای فارسی را UTF-8 درست رمزگذاری می‌کند """
    return quote(str(text), safe='')


def build_share_links(product_name, absolute_url):
    """
    دیکشنری {کلید پیام‌رسان: دیپ‌لینک اشتراک‌گذاری} برای ۸ پیام‌رسان.
    ترتیب کلیدها همان ترتیب تأییدشده برای نمایش در مودال است:
    ایتا، بله، روبیکا، سروش، آی‌گپ، گپ، تلگرام، واتس‌اپ.
    """
    url_q = _q(absolute_url)
    title_q = _q(product_name)
    return {
        'eitaa': f'https://eitaa.com/share/url?url={url_q}&text={title_q}',
        'bale': f'https://ble.ir/share/url?url={url_q}&text={title_q}',
        'rubika': f'https://rubika.ir/share/url?url={url_q}&text={title_q}',
        'soroush': f'https://splus.ir/share/url?url={url_q}&text={title_q}',
        'igap': f'https://igap.net/share/url?url={url_q}&text={title_q}',
        'gap': f'https://gap.im/share/url?url={url_q}&text={title_q}',
        'telegram': f'https://t.me/share/url?url={url_q}&text={title_q}',
        # واتس‌اپ فقط یک پارامتر «text» دارد؛ عنوان و آدرس باید با هم، یک‌جا و فقط
        # یک‌بار urlencode شوند - وگرنه %20 دستی وسط رشته‌ی از قبل رمزگذاری‌شده تکرار می‌شود
        'whatsapp': f'https://api.whatsapp.com/send?text={_q(f"{product_name} {absolute_url}")}',
    }


def build_og_description(description_text):
    """ توضیح کوتاه og:description: بدون تگ HTML، حداکثر ۱۵۰ کاراکتر؛ اگر محصول توضیح نداشت، متن پیش‌فرض سایت """
    text = strip_tags(description_text or '').strip()
    if not text:
        return OG_DESCRIPTION_FALLBACK
    return truncatechars(text, OG_DESCRIPTION_MAX_LENGTH)
