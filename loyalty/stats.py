"""
ثبت سطح وفاداری مؤثر در رجیستری accounts.stats (Loyalty Phase 5A-2 - Effective Loyalty Index
Abstraction Seam). این فقط یک نقطه‌ی اتصال معماری است؛ در این فاز هیچ منطق داینامیکی
(LoyaltyTier/rank/lifetime_earned) اثری روی خروجی ندارد - عیناً همان رفتار سنتی برگردانده
می‌شود. هدف این است که فازهای بعدی بتوانند بدون لمس promotions/*.py منطق را عوض کنند.

اکیداً هیچ importی از promotions در این فایل نیست - جهت وابستگی پروژه یکنواخت می‌ماند:
همه -> accounts (نگاه کنید accounts/stats.py سرِ فایل برای توضیح کامل این الگو).
"""

from accounts.stats import register


@register('effective_loyalty_index')
def effective_loyalty_index(user):
    return user.get_loyalty_level_index()
