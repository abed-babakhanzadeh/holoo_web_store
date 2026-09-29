"""
ثبت سطح وفاداری مؤثر در رجیستری accounts.stats (Loyalty Phase 5A-2 - Effective Loyalty Index
Abstraction Seam؛ Phase 5B-2 - سیاست Higher-Of + فلگ بازگشت‌پذیر). این تنها نقطه‌ی اتصال
معماری‌ای است که رتبه‌ی داینامیک باشگاه می‌تواند از طریق آن روی صلاحیت پروموشن/کوپن اثر بگذارد؛
promotions/*.py هرگز مستقیم به این فایل یا به loyalty وابسته نیست (نگاه کنید promotions/resolver.py).

پیش‌فرض SiteSettings.loyalty_dynamic_tier_in_eligibility=False یعنی این تابع همچنان دقیقاً همان
رفتار فاز ۵A-2 را دارد - صفر تغییر رفتار پروداکشن تا کسی صریحاً فلگ را روشن کند. خاموش‌کردن فلگ
= بازگشت فوری، بدون دیپلوی/ری‌استارت (همان ابطال کش لحظه‌ای save()ی SiteSettings).

Fail-Safe to Legacy: legacy همیشه *قبل* از هر منطق تازه محاسبه می‌شود و تنها مقدار قابل‌اعتماد
است؛ هر خطای غیرمنتظره در بخش داینامیک (تنظیمات/سطح/کراس‌واک) لاگ می‌شود و همان legacy
برگردانده می‌شود - هرگز استثنا به بیرون (به promotions) نشت نمی‌کند.
"""

import logging

from accounts.stats import register

logger = logging.getLogger(__name__)


@register('effective_loyalty_index')
def effective_loyalty_index(user):
    legacy = user.get_loyalty_level_index()
    try:
        from products.models import SiteSettings

        from .services import get_dynamic_tier_for_user

        if not SiteSettings.cached().loyalty_dynamic_tier_in_eligibility:
            return legacy

        tier = get_dynamic_tier_for_user(user)
        if tier is not None and tier.legacy_equivalent_index is not None:
            return max(legacy, tier.legacy_equivalent_index)
        return legacy
    except Exception:
        logger.exception(
            'محاسبه‌ی سطح مؤثر داینامیک برای کاربر %s ناموفق بود؛ بازگشت امن به سطح سنتی.',
            getattr(user, 'id', None),
        )
        return legacy
