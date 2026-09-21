"""
تأمین‌کننده‌ی باکس «شگفت‌انگیز» (صفحه‌ی اصلی و صفحه‌ی دسته): کدام محصولات مشمول یک تخفیف عمومیِ فعال‌اند.

فقط تخفیف‌های *عمومی* (بدون شرط ورود/سطح وفاداری/سطح قیمت/روش پرداخت) که show_in_flash_deals دارند تبلیغ می‌شوند؛
تخفیف‌های شخصی‌شده روی قیمتِ کاربرِ مشمول اعمال می‌شود ولی در باکس همگانی دیده نمی‌شود. قیمتِ نمایش‌داده‌شده روی کارت
همیشه از موتور قیمت می‌آید (مثلاً کاربر ویژه با apply_to_vip خاموش تخفیف نمی‌بیند).
"""

from django.db.models import Q
from django.utils import timezone

from products.models import Product

from .index import get_index

MAX_RULES_CHECKED = 50


def rule_product_filter(rule):
    """ Q روی Product برای محصولات مشمول یک تخفیف (اهداف شمول منهای استثناها) """
    if rule.include_all:
        include = Q(pk__isnull=False)
    else:
        include = Q(pk__in=rule.include_products) if rule.include_products else Q(pk__in=[])
        if rule.include_categories:
            include |= Q(category_id__in=rule.include_categories)
        if rule.include_brands:
            include |= Q(brand_id__in=rule.include_brands)
    if rule.exclude_products:
        include &= ~Q(pk__in=rule.exclude_products)
    if rule.exclude_categories:
        include &= ~Q(category_id__in=rule.exclude_categories)
    if rule.exclude_brands:
        include &= ~Q(brand_id__in=rule.exclude_brands)
    return include


def flash_deals_filter(category_ids=None, now=None):
    """
    (Q روی Product یا None، نزدیک‌ترین زمان پایان بین تخفیف‌های مشمول) برای باکس شگفت‌انگیز.
    category_ids: محدود کردن به یک دسته و زیردسته‌هایش (صفحه‌ی دسته).
    """
    now = now or timezone.now()
    index = get_index()
    if not index.policy.promotions_enabled:
        return None, None

    rules = sorted(
        (r for r in index.rules if r.show_in_flash_deals and r.is_public and r.in_window(now)),
        key=lambda r: (r.ends_at, r.id),
    )[:MAX_RULES_CHECKED]

    combined, nearest_ends_at = None, None
    for rule in rules:
        rule_q = rule_product_filter(rule)
        scope = Product.visible.filter(rule_q)
        if category_ids is not None:
            scope = scope.filter(category_id__in=category_ids)
        if not scope.exists():                    # تخفیفی که هیچ کالای قابل‌نمایشی ندارد تایمر را تعیین نکند
            continue
        combined = rule_q if combined is None else (combined | rule_q)
        if nearest_ends_at is None or rule.ends_at < nearest_ends_at:
            nearest_ends_at = rule.ends_at
    return combined, nearest_ends_at
