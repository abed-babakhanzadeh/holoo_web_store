"""
ترتیب‌های مجاز نمایش نظرات.

تعریف واحد: قبلاً این دیکشنری عیناً هم در reviews/views.py و هم در products/views.py
تکرار شده بود و تغییر یکی بدون دیگری، ترتیب نظرات صفحه‌ی محصول را با پنل کاربری ناهماهنگ می‌کرد.
"""

REVIEW_SORT_OPTIONS = {
    'newest': ('-created_at',),
    'oldest': ('created_at',),
    'rating_high': ('-rating', '-created_at'),
    'rating_low': ('rating', '-created_at'),
}

DEFAULT_REVIEW_SORT = 'newest'


def review_order_by(sort):
    """ تاپل order_by متناظر با مقدار sort ورودی کاربر (مقدار نامعتبر -> پیش‌فرض) """
    return REVIEW_SORT_OPTIONS.get(sort, REVIEW_SORT_OPTIONS[DEFAULT_REVIEW_SORT])
