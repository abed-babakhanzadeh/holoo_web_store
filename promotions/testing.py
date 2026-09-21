"""
ابزارهای تست تخفیف (برای تست‌های همه‌ی اپ‌ها).

نکته‌ی مهم: شاخص تخفیف‌ها در Redis/حافظه کش می‌شود و rollbackِ تراکنش TestCase سیگنال حذف نمی‌فرستد؛ پس هر تستی که
Promotion می‌سازد باید کش را پایان تست هم پاک کند وگرنه تخفیفِ «شبح» به تست‌های بعدی نشت می‌کند. make_promotion()
و PromotionTestMixin این را خودکار انجام می‌دهند.
"""

from datetime import timedelta

from django.utils import timezone

from . import index
from .models import Promotion, PromotionTarget


def reset_promotions_cache():
    index.invalidate()


class PromotionTestMixin:
    """ به هر TestCase که تخفیف می‌سازد اضافه شود: کش شاخص قبل و بعد از هر تست پاک می‌شود """

    def setUp(self):
        super().setUp()
        reset_promotions_cache()
        self.addCleanup(reset_promotions_cache)


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
