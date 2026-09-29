"""
تست‌های Loyalty Phase 6B: تگ‌های نمایشیِ فروشگاه (loyalty/templatetags/loyalty_tags.py) -
بج امتیاز صفحه‌ی محصول و برآورد امتیاز فاکتور چک‌اوت.

عمداً هیچ‌کدام از این تست‌ها به دیتابیس لجر (LoyaltyAccount/LoyaltyTransaction) وابسته نیستند -
این تگ‌ها صرفاً از روی SiteSettings و قیمت زنده‌ی محصول محاسبه می‌کنند، بدون خواندن هیچ حسابی.
"""

import itertools
from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from accounts.testing import make_approved_user
from loyalty.templatetags.loyalty_tags import order_loyalty_points_estimate, product_loyalty_points
from products.models import Category, Product, SiteSettings

_seq = itertools.count(1)


class StorefrontTagsTestBase(TestCase):
    def setUp(self):
        super().setUp()
        cache.delete(SiteSettings.CACHE_KEY)
        SiteSettings.load().save()
        self.addCleanup(self._reset_settings)

        self.category = Category.objects.create(name='دسته ۶B', slug=f'cat-6b-{next(_seq)}')
        self.product = Product.objects.create(
            name='کالای ۶B', slug=f'p-6b-{next(_seq)}', erp_code=f'ERP-6B-{next(_seq)}',
            category=self.category, price=250000, stock=10,
        )
        self.user = make_approved_user(f'0912081{next(_seq):04d}', price_level=1)

    def _reset_settings(self):
        obj = SiteSettings.load()
        obj.loyalty_mode = SiteSettings.LOYALTY_MODE_ORDER_COUNT
        obj.loyalty_amount_step = 100000
        obj.loyalty_points_per_order = 100
        obj.loyalty_activated_at = None
        obj.save()
        cache.delete(SiteSettings.CACHE_KEY)

    def _activate_club(self, mode):
        obj = SiteSettings.load()
        obj.loyalty_activated_at = timezone.now() - timedelta(days=1)
        obj.loyalty_mode = mode
        obj.loyalty_amount_step = 100000
        obj.loyalty_points_per_order = 100
        obj.save()
        cache.delete(SiteSettings.CACHE_KEY)


class ProductLoyaltyPointsTagTests(StorefrontTagsTestBase):
    def test_returns_none_when_club_is_not_activated(self):
        self.assertIsNone(product_loyalty_points(self.product, self.user))

    def test_returns_none_in_order_count_mode(self):
        """ در حالت «تعداد سفارش»، امتیاز به کل سفارش تعلق دارد نه یک قلم - بج نباید نمایش یابد. """
        self._activate_club(SiteSettings.LOYALTY_MODE_ORDER_COUNT)
        self.assertIsNone(product_loyalty_points(self.product, self.user))

    def test_returns_points_in_amount_mode(self):
        self._activate_club(SiteSettings.LOYALTY_MODE_AMOUNT)
        # قیمت کالا ۲۵۰,۰۰۰ ÷ گام ۱۰۰,۰۰۰ = ۲ امتیاز
        self.assertEqual(product_loyalty_points(self.product, self.user), 2)

    def test_returns_none_when_price_is_too_low_for_one_point(self):
        self._activate_club(SiteSettings.LOYALTY_MODE_AMOUNT)
        cheap = Product.objects.create(
            name='ارزان', slug=f'cheap-{next(_seq)}', erp_code=f'ERP-CHEAP-{next(_seq)}',
            category=self.category, price=1000, stock=10,
        )
        self.assertIsNone(product_loyalty_points(cheap, self.user))


class OrderLoyaltyPointsEstimateTagTests(StorefrontTagsTestBase):
    def test_returns_none_when_club_is_not_activated(self):
        self.assertIsNone(order_loyalty_points_estimate(500000))

    def test_amount_mode_divides_items_total_by_step(self):
        self._activate_club(SiteSettings.LOYALTY_MODE_AMOUNT)
        self.assertEqual(order_loyalty_points_estimate(350000), 3)

    def test_amount_mode_returns_none_below_one_step(self):
        self._activate_club(SiteSettings.LOYALTY_MODE_AMOUNT)
        self.assertIsNone(order_loyalty_points_estimate(50000))

    def test_order_count_mode_returns_the_flat_value_regardless_of_amount(self):
        self._activate_club(SiteSettings.LOYALTY_MODE_ORDER_COUNT)
        self.assertEqual(order_loyalty_points_estimate(999999), 100)
