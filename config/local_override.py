"""
تنظیمات مخصوص هر ماشین (سرور/توسعه) بدون دست‌زدن به config/settings.py.

مشکل: مقدارهایی مثل رمز دیتابیس، پورت یا دامنه‌ی CSRF روی سرور با توسعه فرق دارند؛ پس settings.py روی سرور دستی ویرایش می‌شد و هر `git pull`
که settings.py را عوض می‌کرد با «local changes would be overwritten» گیر می‌کرد.

راه‌حل: فایل config/local_settings.py (در گیت نیست، .gitignore) اگر وجود داشته باشد، در پایان settings.py *داخل همان فضای نام* اجرا می‌شود؛
یعنی می‌تواند مقدار را عوض کند (`CSRF_TRUSTED_ORIGINS = [...]`) یا جزئی ویرایش کند (`DATABASES['default']['PASSWORD'] = '...'`) و حتی
متغیرهای پایه را بسازد. چون settings.py روی سرور دیگر ویرایش نمی‌شود، pull همیشه بی‌مشکل است. نمونه: config/local_settings.example.py.

نکته: مقدارهایی که settings.py پیش‌تر از روی متغیری ساخته (مثل CELERY_BROKER_URL و CACHES از REDIS_URL) با عوض‌کردن آن متغیر در این فایل
دوباره ساخته نمی‌شوند؛ برای آن‌ها متغیر محیطی REDIS_URL را بگذارید (settings.py آن را از محیط می‌خواند) یا خود آن مقدار را این‌جا بازنویسی کنید.
"""

from pathlib import Path


def apply_local_settings(namespace, path):
    """ اگر فایل path وجود داشت، آن را داخل namespace (معمولاً globals() فایل settings) اجرا می‌کند؛ نبودنش خطا نیست. خروجی: True اگر اجرا شد """
    path = Path(path)
    if not path.is_file():
        return False
    source = path.read_text(encoding='utf-8-sig')
    exec(compile(source, str(path), 'exec'), namespace)         # خطای نحوی/اجرایی عمداً بالا می‌آید تا سرور با تنظیم خراب بالا نیاید
    return True
