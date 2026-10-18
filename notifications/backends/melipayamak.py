"""
موتور پیامک ملی‌پیامک — دو خط با دو وب‌سرویس متفاوت:

  ۱. خط خدماتی اشتراکی (BaseServiceNumber): فقط بدنه‌های (Body) از پیش در پنل
     ملی‌پیامک تاییدشده را قبول می‌کند و متغیرهایش را باید جدا از هم (نه متن نهایی
     رندرشده) با ; و به همان ترتیبی که در پنل تعریف شده بفرستیم. TEMPLATE_MAP همین
     نگاشت (template_key پروژه -> bodyId + ترتیب متغیرها) را نگه می‌دارد.
  ۲. خط اختصاصی کارفرما (SmartSMS): متن آزاد قبول می‌کند، دقیقاً مثل کاوه‌نگار.

send_template ابتدا TEMPLATE_MAP را چک می‌کند؛ اگر قالبی آنجا تعریف نشده بود (مثلاً چون
هنوز در پنل تایید نشده یا اصلاً محتوایش متغیر/آزاد است، مثل هشدارهای بحرانی سیستم)،
به‌جای شکست، همان متن نهایی را از خط اختصاصی (send) می‌فرستد. یعنی افزودن یک قالب تازه
به notifications/templates_registry.py بدون هیچ تغییری در این فایل هم کار می‌کند —
فقط اگر خواستید هزینه‌ی ارزان‌تر/تاییدشده‌ی خط اشتراکی را برایش هم بگیرید، یک ردیف به
TEMPLATE_MAP اضافه کنید.

نکته‌ی مهم درباره‌ی خط اختصاصی: طبق مستندات ملی‌پیامک، عدم وجود «لغو11» در انتهای متن
پیامک‌های تبلیغاتی باعث رد شدن با کد خطای 15 می‌شود. اگر این خط برای پیامک خدماتی/تراکنشی
(نه تبلیغاتی) نزد اپراتور ثبت شده باشد معمولاً این الزام برداشته می‌شود؛ اگر بعداً به
خطای 15 برخوردید، با پشتیبانی ملی‌پیامک نوع ثبت خط را چک کنید.

اسناد رسمی: راهنمای وب‌سرویس Rest ملی‌پیامک (BaseServiceNumber) و راهنمای وب‌سرویس
هوشمند SmartSMS (متد Send).
"""
import logging

import requests
from django.conf import settings

from .base import NotificationBackend, NotificationBackendError, NotificationPermanentError

logger = logging.getLogger(__name__)

BASE_NUMBER_URL = 'https://rest.payamak-panel.com/api/SendSMS/BaseServiceNumber'
SMART_SEND_URL = 'https://rest.payamak-panel.com/api/SmartSMS/Send'
TIMEOUT = 10

# template_key -> (bodyId تاییدشده در پنل خط اشتراکی، ترتیب متغیرها طبق همان قالب)
TEMPLATE_MAP = {
    'otp': (537452, ('code',)),
    'profile_completed_admin': (537945, ('full_name', 'phone')),
    'payment_succeeded_admin': (537944, ('order_id', 'amount', 'phone')),
    'payment_succeeded_customer': (537943, ('name', 'amount', 'ref_id')),
    'order_placed_customer': (537931, ('name', 'order_id')),
    'order_shipped_customer': (537933, ('name', 'tracking_code')),
    # holoo_sync_stalled_admin و critical_alert عمداً اینجا نیستند: اولی روی خط اشتراکی
    # رد شد (نیاز به تایید پشتیبانی برای متغیرها) و دومی اصلاً متن آزاد/متغیر است. هر دو
    # حالا خودکار از طریق send() با خط اختصاصی می‌روند.
}


def _credentials():
    username = getattr(settings, 'MELIPAYAMAK_USERNAME', '')
    # طبق راهنمای ملی‌پیامک، وقتی حساب روی «الزام ApiKey» است، ApiKey به‌جای رمز عبور
    # در همین فیلد password فرستاده می‌شود.
    password = getattr(settings, 'MELIPAYAMAK_APIKEY', '')
    if not username or not password:
        raise NotificationBackendError("MELIPAYAMAK_USERNAME یا MELIPAYAMAK_APIKEY در تنظیمات پر نشده است.")
    return username, password


# ملی‌پیامک خطای ارسال را با RetStatus=1 و یک «کد کوتاه» در Value برمی‌گرداند؛ ارسال موفق یک شناسه‌ی بلند (رقمی ≥ ۷ رقم) است.
# ممیزی ۱۸ اکتبر ۲۰۲۶: پیام‌های خبرنامه (حاوی لینک) کد ۱۴ می‌گرفتند ولی چون RetStatus=1 بود «ارسال‌شده» ثبت می‌شدند و هرگز نمی‌رسیدند.
# کد ← (متن فارسی، دائمی؟). کدهای دائمی را تلاش دوباره درست نمی‌کند؛ بقیه (اعتبار، محدودیت نرخ، بروزرسانی سامانه) با retry ادامه می‌یابند.
ERROR_CODES = {
    '0': ('نام کاربری یا ApiKey ملی‌پیامک اشتباه است', True),
    '2': ('اعتبار حساب ملی‌پیامک کافی نیست', False),
    '3': ('محدودیت ارسال روزانه', False),
    '4': ('محدودیت حجم ارسال', False),
    '5': ('شماره‌ی خط فرستنده معتبر نیست', True),
    '6': ('سامانه‌ی ملی‌پیامک در حال بروزرسانی است', False),
    '7': ('متن حاوی کلمه‌ی فیلترشده است', True),
    '9': ('ارسال از خط عمومی مجاز نیست', True),
    '10': ('کاربر ملی‌پیامک غیرفعال است', True),
    '11': ('پیام ارسال نشد', False),
    '12': ('مدارک کاربر در ملی‌پیامک کامل نیست', True),
    '14': ('متن حاوی لینک است و این خط لینک را نمی‌پذیرد؛ لینک را از متن حذف کنید', True),
    '15': ('متن تبلیغاتی باید به «لغو11» ختم شود', True),
    '16': ('شماره‌ی گیرنده پیدا نشد', True),
    '17': ('متن پیام خالی است', True),
    '18': ('شماره‌ی گیرنده نامعتبر است', True),
}
MAX_ERROR_CODE_LENGTH = 3


def _post(url, payload):
    try:
        response = requests.post(url, data=payload, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise NotificationBackendError(f"خطای شبکه در ارتباط با ملی‌پیامک: {e}") from e

    if response.status_code != 200:
        raise NotificationBackendError(f"ملی‌پیامک کد {response.status_code} برگرداند: {response.text[:200]}")

    try:
        data = response.json()
    except ValueError as e:
        raise NotificationBackendError(f"پاسخ ملی‌پیامک JSON معتبر نبود: {response.text[:200]}") from e

    if data.get('RetStatus') != 1:
        raise NotificationBackendError(f"ملی‌پیامک پیام را رد کرد ({data.get('StrRetStatus')}): {data.get('Value')}")

    value = str(data.get('Value') or '')
    if value.isdigit() and len(value) <= MAX_ERROR_CODE_LENGTH:
        message, permanent = ERROR_CODES.get(value.lstrip('0') or '0', (f'کد خطای نامشخص {value}', False))
        raise (NotificationPermanentError if permanent else NotificationBackendError)(f"ملی‌پیامک پیام را نفرستاد (کد {value}): {message}")
    return value


class MelipayamakBackend(NotificationBackend):
    def send(self, to: str, text: str) -> str:
        """ متن آزاد از خط اختصاصی کارفرما (SmartSMS) """
        username, password = _credentials()
        from_number = getattr(settings, 'MELIPAYAMAK_FROM_NUMBER', '')
        if not from_number:
            raise NotificationBackendError("MELIPAYAMAK_FROM_NUMBER (شماره خط اختصاصی) در تنظیمات پر نشده است.")

        return _post(SMART_SEND_URL, {
            'username': username, 'password': password, 'to': to, 'text': text, 'from': from_number,
        })

    def send_template(self, to: str, template_key: str, context: dict, text: str) -> str:
        mapping = TEMPLATE_MAP.get(template_key)
        if mapping is None:
            # قالبی که روی خط اشتراکی تایید نشده؛ به‌جای شکست، متن نهایی از خط اختصاصی می‌رود
            return self.send(to, text)

        body_id, var_names = mapping
        try:
            variables = [str(context[name]) for name in var_names]
        except KeyError as e:
            raise NotificationBackendError(f"متغیر {e} برای قالب «{template_key}» در context موجود نیست.") from e

        username, password = _credentials()
        return _post(BASE_NUMBER_URL, {
            'username': username, 'password': password, 'text': ';'.join(variables), 'to': to, 'bodyId': body_id,
        })
