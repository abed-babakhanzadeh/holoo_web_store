"""
قفل توزیع‌شده‌ی سبک روی کش مشترک (Redis)، برای تسک‌هایی که نباید دو نسخه‌شان هم‌زمان اجرا شود.

چرا لازم است:
  - سینک محصولات هر ۲۵ دقیقه شلیک می‌شود ولی برای کاتالوگ چندهزارتایی می‌تواند بیشتر طول
    بکشد؛ بدون قفل، دو اجرا روی هم می‌افتند و get_or_create روی erp_code یکتا خطا می‌دهد.
  - ثبت فاکتور/سند دریافت وجه ممکن است هم از مسیر عادی و هم از تسک بازبینی شلیک شود؛ بدون
    قفل، دو Worker می‌توانند هم‌زمان قبل از ذخیره شدن کدِ برگشتی، دو سند در حسابداری بسازند.

نکته: timeout نقش تور ایمنی دارد؛ اگر Worker وسط کار کشته شود و finally اجرا نشود، قفل
خودش پس از timeout آزاد می‌شود و سیستم قفل‌شده باقی نمی‌ماند.
"""

import uuid
from contextlib import contextmanager

from django.core.cache import cache


@contextmanager
def task_lock(key, timeout=600):
    """
    اگر قفل گرفته شود True و در غیر این صورت False به بدنه‌ی with می‌دهد:

        with task_lock('holoo:invoice:12') as acquired:
            if not acquired:
                return 'skipped'
            ...
    """
    cache_key = f'lock:{key}'
    token = uuid.uuid4().hex
    acquired = cache.add(cache_key, token, timeout)
    try:
        yield acquired
    finally:
        # فقط قفلِ خودمان را آزاد می‌کنیم: اگر قفل ما منقضی شده و اجرای دیگری قفل تازه گرفته، پایان کار ما آن را نمی‌پرانَد.
        # (کش Redis مقایسه-و-حذف اتمیک ندارد؛ پنجره‌ی باقی‌مانده فقط فاصله‌ی همین دو خط است، نه کل مدت کار)
        if acquired and cache.get(cache_key) == token:
            cache.delete(cache_key)
