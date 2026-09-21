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


# provider(user, queryset, now=None) -> شیءِ کاتالوگ تخفیف با count / filter_queryset(qs) / ordered_ids(qs)
# (فیلتر «فقط کالاهای دارای تخفیف» و مرتب‌سازی «بیشترین تخفیف» در لیست محصولات)
_catalog_provider = None


def register_discount_catalog_provider(provider):
    global _catalog_provider
    _catalog_provider = provider


def discount_catalog_available():
    return _catalog_provider is not None


def discount_catalog(user, queryset, now=None):
    """ کاتالوگ تخفیفِ همین کاربر روی کوئری‌ست، یا None اگر تأمین‌کننده‌ای ثبت نشده باشد """
    if _catalog_provider is None:
        return None
    return _catalog_provider(user, queryset, now)


def flash_deals_filter(category_ids=None, now=None):
    """ (Q، ends_at) یا (None، None) اگر تأمین‌کننده‌ای ثبت نشده یا تخفیفی نیست """
    if _provider is None:
        return None, None
    return _provider(category_ids=category_ids, now=now)
