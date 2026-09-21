"""
رجیستری تأمین‌کننده‌ی «محصولات دارای تخفیف» برای باکس شگفت‌انگیز.

اپ products به اپ تخفیف‌ها (promotions) وابسته نیست؛ promotions در ready() تأمین‌کننده‌اش را اینجا ثبت می‌کند
(همان الگوی products/blog_posts.py و products/pricing.register_promotion_resolver).
"""

# provider(category_ids=None, now=None) -> (Q روی Product یا None، نزدیک‌ترین زمان پایان یا None)
_provider = None


def register_flash_deals_provider(provider):
    global _provider
    _provider = provider


def flash_deals_filter(category_ids=None, now=None):
    """ (Q، ends_at) یا (None، None) اگر تأمین‌کننده‌ای ثبت نشده یا تخفیفی نیست """
    if _provider is None:
        return None, None
    return _provider(category_ids=category_ids, now=now)
