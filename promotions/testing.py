"""
ابزارهای تست تخفیف (برای تست‌های همه‌ی اپ‌ها).

نکته‌ی مهم: شاخص تخفیف‌ها در Redis/حافظه کش می‌شود و rollbackِ تراکنش TestCase سیگنال حذف نمی‌فرستد؛ پس هر تستی که
Promotion می‌سازد باید کش را پایان تست هم پاک کند وگرنه تخفیفِ «شبح» به تست‌های بعدی نشت می‌کند. make_promotion()
و PromotionTestMixin این را خودکار انجام می‌دهند.
"""

from datetime import timedelta

from django.utils import timezone

from . import free_shipping, index, ratelimit
from .models import Coupon, FreeShippingRule, Promotion, PromotionTarget

TEST_CLIENT_IP = '127.0.0.1'


def reset_promotions_cache():
    index.invalidate()
    free_shipping.invalidate()


def reset_coupon_attempts(*users):
    """ شمارنده‌های تلاش ناموفق کد (Redis مشترک است و شناسه‌ی کاربرِ تست بین اجراها تکرار می‌شود) """
    ratelimit.reset(None, TEST_CLIENT_IP)
    for user in users:
        ratelimit.reset(user, None)


class PromotionTestMixin:
    """ به هر TestCase که تخفیف می‌سازد اضافه شود: کش شاخص قبل و بعد از هر تست پاک می‌شود """

    def setUp(self):
        super().setUp()
        reset_promotions_cache()
        reset_coupon_attempts()
        self.addCleanup(reset_promotions_cache)
        self.addCleanup(reset_coupon_attempts)


def make_promotion(product=None, *, percent=25, kind=Promotion.KIND_PERCENT, value=None, active=True, expired=False,
                   scheduled=False, targets=None, title=None, **fields):
    """
    تخفیف آزمایشی. پیش‌فرض: درصدیِ ۲۵٪ روی `product` که از دیروز شروع شده و تا فردا فعال است.
    targets: فهرست دیکشنری‌های PromotionTarget (به‌جای product) مثل {'target_type': 'category', 'category': c}
    """
    now = timezone.now()
    if expired:
        starts_at, ends_at = now - timedelta(days=2), now - timedelta(days=1)
    elif scheduled:
        starts_at, ends_at = now + timedelta(days=1), now + timedelta(days=2)
    else:
        starts_at, ends_at = now - timedelta(days=1), now + timedelta(days=1)
    data = dict(
        title=title or f'تخفیف آزمایشی {percent if value is None else value}', kind=kind,
        value=percent if value is None else value, starts_at=starts_at, ends_at=ends_at, is_active=active,
    )
    data.update(fields)
    promotion = Promotion.objects.create(**data)
    for target in (targets if targets is not None else [{'target_type': 'product', 'product': product}]):
        PromotionTarget.objects.create(promotion=promotion, **target)
    reset_promotions_cache()
    return promotion


def make_coupon(code='TESTCODE', *, kind=Coupon.KIND_PERCENT, value=10, products=(), categories=(), active=True, expired=False,
                scheduled=False, **fields):
    """
    کد تخفیف آزمایشی؛ پیش‌فرض: درصدی ۱۰٪ روی کل سبد، فعال و بدون محدودیت زمانی، سقف هر کاربر ۱.
    products/categories اگر داده شوند شمول را «محصولات/دسته‌های انتخاب‌شده» می‌کنند.
    """
    now = timezone.now()
    data = dict(title=f'کد آزمایشی {code}', kind=kind, value=value if kind != Coupon.KIND_FREE_SHIPPING else 0, is_active=active)
    if expired:
        data.update(starts_at=now - timedelta(days=2), ends_at=now - timedelta(days=1))
    elif scheduled:
        data.update(starts_at=now + timedelta(days=1), ends_at=now + timedelta(days=2))
    if products or categories:
        data['scope'] = Coupon.SCOPE_ITEMS
    data.update(fields)
    coupon = Coupon.objects.create(code=code, **data)
    if products:
        coupon.products.set(products)
    if categories:
        coupon.categories.set(categories)
    return coupon


def make_free_shipping_rule(title='ارسال رایگان آزمایشی', *, min_total=0, provinces=(), cities=(), active=True, expired=False,
                            scheduled=False, **fields):
    """ قاعده‌ی ارسال رایگان آزمایشی؛ پیش‌فرض: کل کشور، بدون حداقل و بدون محدودیت زمانی """
    now = timezone.now()
    data = dict(title=title, min_cart_total=min_total, is_active=active)
    if expired:
        data.update(starts_at=now - timedelta(days=3), ends_at=now - timedelta(days=2))
    elif scheduled:
        data.update(starts_at=now + timedelta(days=1), ends_at=now + timedelta(days=2))
    data.update(fields)
    rule = FreeShippingRule.objects.create(**data)
    if provinces:
        rule.provinces.set(provinces)
    if cities:
        rule.cities.set(cities)
    reset_promotions_cache()
    return rule
