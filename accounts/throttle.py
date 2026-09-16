"""
محدودیت نرخ ارسال کد یکبارمصرف.

تا پیش از این، SendOTPView هیچ محدودیتی نداشت: می‌شد بی‌نهایت کد برای یک شماره فرستاد.
با موتور پیامک واقعی این مستقیم هزینه‌ی مالی دارد (هر ارسال پول است) و برای صاحب شماره
هم آزاردهنده است. چون VerifyOTPView کاربر را با get_or_create می‌سازد، ساخت انبوه حساب
هم از همین مسیر ممکن بود.

سه سقف هم‌زمان اعمال می‌شود:
  - فاصله‌ی حداقلی بین دو درخواست برای یک شماره (جلوی اسپم دکمه‌ی «ارسال مجدد»)
  - سقف تعداد در ساعت برای یک شماره
  - سقف تعداد در ساعت برای یک IP (جلوی پیمایش روی شماره‌های مختلف)

شمارنده‌ها روی کش مشترک (Redis) هستند، پس بین همه‌ی Workerها معتبرند.
"""

from django.core.cache import cache

# فاصله‌ی حداقلی بین دو کد برای یک شماره
RESEND_COOLDOWN = 60  # ثانیه

# سقف در بازه‌ی یک ساعته
MAX_PER_PHONE_PER_HOUR = 5
MAX_PER_IP_PER_HOUR = 15
WINDOW = 3600


class ThrottleError(Exception):
    """ پیام آماده برای نمایش به کاربر """


def get_client_ip(request):
    """
    IP واقعی کاربر.

    نکته‌ی استقرار: اگر روزی سایت پشت nginx/کلادفلر رفت، باید X-Forwarded-For خوانده شود —
    ولی فقط وقتی تعداد پراکسی‌های مورد اعتماد مشخص باشد، وگرنه کاربر می‌تواند هدر جعلی
    بفرستد و محدودیت IP را کاملاً دور بزند. تا آن زمان عمداً فقط REMOTE_ADDR خوانده می‌شود.
    """
    return request.META.get('REMOTE_ADDR') or 'unknown'


def _incr(key, window):
    """ شمارنده‌ی پنجره‌ای؛ مقدار جدید را برمی‌گرداند """
    # cache.add فقط وقتی کلید وجود ندارد مقدار می‌گذارد، پس TTL پنجره با اولین درخواست
    # تثبیت می‌شود و با درخواست‌های بعدی تمدید (و پنجره بی‌نهایت) نمی‌شود
    if cache.add(key, 1, window):
        return 1
    try:
        return cache.incr(key)
    except ValueError:
        # کلید دقیقاً بین add و incr منقضی شد
        cache.set(key, 1, window)
        return 1


def check_otp_quota(phone_number, ip):
    """
    اجازه‌ی ارسال یک کد تازه را بررسی می‌کند.
    در صورت عبور از سقف، ThrottleError با پیام فارسی مناسب می‌اندازد.
    فقط بررسی می‌کند و چیزی مصرف نمی‌کند؛ مصرف با consume_otp_quota انجام می‌شود.
    """
    if cache.get(f'otp:cooldown:{phone_number}'):
        raise ThrottleError(
            f'برای دریافت کد جدید کمی صبر کنید (حداکثر {RESEND_COOLDOWN} ثانیه).'
        )

    if (cache.get(f'otp:phone:{phone_number}') or 0) >= MAX_PER_PHONE_PER_HOUR:
        raise ThrottleError(
            'تعداد درخواست کد برای این شماره در یک ساعت گذشته بیش از حد مجاز است. '
            'لطفاً بعداً دوباره تلاش کنید.'
        )

    if (cache.get(f'otp:ip:{ip}') or 0) >= MAX_PER_IP_PER_HOUR:
        raise ThrottleError(
            'تعداد درخواست‌ها از این دستگاه بیش از حد مجاز است. لطفاً بعداً دوباره تلاش کنید.'
        )


def consume_otp_quota(phone_number, ip):
    """ پس از ارسال موفق کد، شمارنده‌ها را افزایش می‌دهد """
    cache.set(f'otp:cooldown:{phone_number}', 1, RESEND_COOLDOWN)
    _incr(f'otp:phone:{phone_number}', WINDOW)
    _incr(f'otp:ip:{ip}', WINDOW)


def reset_otp_quota(phone_number):
    """ پس از ورود موفق، سقف شماره آزاد می‌شود (کاربر واقعی نباید بعداً قفل بماند) """
    cache.delete_many([f'otp:cooldown:{phone_number}', f'otp:phone:{phone_number}'])
