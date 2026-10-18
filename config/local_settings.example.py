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

# --- اختیاری ---
# DEBUG = False
# ALLOWED_HOSTS = ['mosavimart.ir', 'www.mosavimart.ir']
