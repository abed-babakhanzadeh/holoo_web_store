# نمونه‌ی config/local_settings.py — این فایل را کپی کنید با نام local_settings.py (در گیت نیست) و فقط مقدارهای مخصوص همان ماشین را بنویسید.
# داخل فضای نام settings.py اجرا می‌شود: هر متغیر settings (مثل DATABASES) در دسترس است. رمزها را هرگز در گیت نگذارید.
# توضیح کامل: config/local_override.py

# --- سرور (دامنه‌ی HTTPS پشت پراکسی) ---
CSRF_TRUSTED_ORIGINS = [
    'https://mosavimart.ir',
    'https://www.mosavimart.ir',
]

# --- دیتابیس: فقط فیلدهای متفاوت را عوض کنید (بقیه از settings.py می‌ماند) ---
DATABASES['default'].update({
    'PASSWORD': 'رمز-دیتابیس-این-ماشین',
    'PORT': '1433',
})

# --- Redis (کش و Celery) ---
# آدرس پیش‌فرض Redis در settings.py مال ماشین توسعه است. چون CELERY_BROKER_URL و CACHES در settings.py از روی REDIS_URL ساخته شده‌اند،
# عوض‌کردن فقط REDIS_URL کافی نیست؛ سه مقدار زیر را هم دوباره بسازید (CACHE_REDIS_DB در settings.py تعریف شده است).
# اگر Redis روی همین سرور است:
# REDIS_URL = 'redis://127.0.0.1:6379'
# CELERY_BROKER_URL = f'{REDIS_URL}/0'
# CELERY_RESULT_BACKEND = f'{REDIS_URL}/0'
# CACHES['default']['LOCATION'] = f'{REDIS_URL}/{CACHE_REDIS_DB}'

# --- اختیاری ---
# DEBUG = False
# ALLOWED_HOSTS = ['mosavimart.ir', 'www.mosavimart.ir']
