"""
تست‌های Loyalty Phase 5B-2: سیاست Higher-Of و فلگ بازگشت‌پذیر در effective_loyalty_index
(loyalty/stats.py) + یکپارچه‌سازی با پروموشن/کوپن (promotions/resolver.py، بدون هیچ تغییری
در promotions/*.py).

هیچ استثنایی نباید از effective_loyalty_index نشت کند (Fail-Safe to Legacy) - این فایل فقط
رفتار موفق را می‌سنجد؛ تست‌های ۵A-2 (loyalty/tests_stats.py) همچنان اثبات می‌کنند فلگ در حالت
پیش‌فرض (False) هیچ اثری ندارد.
"""

import itertools

from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from unittest import mock

from accounts.models import CustomUser
from accounts.testing import make_approved_user
from cart.models import Cart, CartItem
from cart.pricing import price_cart
from loyalty import services
from loyalty.models import LoyaltyTier, LoyaltyTransaction
from loyalty.stats import effective_loyalty_index
from orders.models import Order, OrderItem
from payments.models import Transaction
from products.models import Category, Product, SiteSettings
from products.pricing import CHECK
from promotions.coupons import evaluate_coupon
from promotions.resolver import _loyalty_index
from promotions.testing import PromotionTestMixin, make_coupon

_seq = itertools.count(1)


def _make_paid_order(user):
    order = Order.objects.create(
        user=user, first_name='کاربر', last_name='تست', phone=user.phone_number,
        address='تهران', payment_method='cash', total_price=0,
    )
    Transaction.objects.create(
        user=user, order=order, amount=order.total_price,
        authority=f'TEST-5B2-{next(_seq)}', status='success',
    )
    return order


class EffectiveTierTestBase(TestCase):
    """ هم‌الگوی loyalty/tests_stats.py::LoyaltyStatsTestBase - آستانه‌های سنتی و فلگ را دترمینیستیک می‌کند. """

    def setUp(self):
        super().setUp()
        cache.delete(SiteSettings.CACHE_KEY)
        SiteSettings.load().save()
        LoyaltyTier.objects.all().delete()
        self.addCleanup(self._reset_settings)
        self.addCleanup(LoyaltyTier.objects.all().delete)

    def _reset_settings(self):
        obj = SiteSettings.load()
        obj.loyalty_mode = SiteSettings.LOYALTY_MODE_ORDER_COUNT
        obj.loyalty_points_per_order = 100
        obj.loyalty_threshold_bronze = 300
        obj.loyalty_threshold_silver = 700
        obj.loyalty_threshold_gold = 1500
        obj.loyalty_threshold_diamond = 3000
        obj.loyalty_activated_at = None
        obj.loyalty_dynamic_tier_in_eligibility = False
        obj.save()
        cache.delete(SiteSettings.CACHE_KEY)

    def set_flag(self, value):
        obj = SiteSettings.load()
        obj.loyalty_dynamic_tier_in_eligibility = value
        obj.save()
        cache.delete(SiteSettings.CACHE_KEY)

    def fresh(self, user):
        return CustomUser.objects.get(pk=user.pk)

    def make_user_with_legacy_level(self, paid_orders):
        user = CustomUser.objects.create_user(phone_number=f'0912077{next(_seq):04d}')
        for _ in range(paid_orders):
            _make_paid_order(user)
        return self.fresh(user)

    def make_tiers(self):
        """ ۴ سطح: پایه(۰)، بدون‌نگاشت(None)، معادل‌۲، معادل‌۳ - آستانه‌ها عمداً گشاد تا کسب امتیاز دقیق ساده بماند. """
        base = LoyaltyTier.objects.create(title='پایه', rank=0, threshold=0, legacy_equivalent_index=0)
        unmapped = LoyaltyTier.objects.create(title='بدون‌نگاشت', rank=1, threshold=500, legacy_equivalent_index=None)
        mapped2 = LoyaltyTier.objects.create(title='معادل۲', rank=2, threshold=1000, legacy_equivalent_index=2)
        mapped3 = LoyaltyTier.objects.create(title='معادل۳', rank=3, threshold=2000, legacy_equivalent_index=3)
        return base, unmapped, mapped2, mapped3


class FlagOffTests(EffectiveTierTestBase):
    """ سناریوی ۱: فلگ خاموش - حتی با بالاترین رتبه‌ی داینامیک، خروجی فقط legacy است. """

    def test_highest_dynamic_tier_has_no_effect_when_flag_is_off(self):
        self.set_flag(False)
        base, unmapped, mapped2, mapped3 = self.make_tiers()
        user = self.make_user_with_legacy_level(0)   # legacy index = 0
        services.credit_points(user, 2000, LoyaltyTransaction.EARN_ACTION, 'رسیدن به بالاترین سطح داینامیک')
        user = self.fresh(user)

        self.assertEqual(user.get_loyalty_level_index(), 0)
        self.assertEqual(effective_loyalty_index(user), 0)   # نه ۳ (نادیده‌گرفتنِ کامل سمت داینامیک)


class FlagOnTransitionMatrixTests(EffectiveTierTestBase):
    """ سناریوی ۲: هر ۵ حالتِ ماتریس ۵A-4، با فلگ روشن. """

    def setUp(self):
        super().setUp()
        self.set_flag(True)
        self.base, self.unmapped, self.mapped2, self.mapped3 = self.make_tiers()

    def test_old_customer_gold_legacy_base_dynamic_keeps_legacy(self):
        """ سطح طلایی سنتی (اندیس ۳) + سطح پایه داینامیک (اندیس ۰) -> ۳ (حفظ کامل حق مکتسبه). """
        user = self.make_user_with_legacy_level(15)   # ۱۵ سفارش = طلایی = اندیس ۳
        self.assertEqual(user.get_loyalty_level_index(), 3)
        self.assertEqual(effective_loyalty_index(user), 3)

    def test_new_customer_no_legacy_high_dynamic_gets_the_dynamic_grant(self):
        """ بدون سابقه‌ی سنتی (اندیس ۰) + سطح داینامیک با نگاشت ۳ -> ۳ (ارتقای واقعی). """
        user = self.make_user_with_legacy_level(0)
        services.credit_points(user, 2000, LoyaltyTransaction.EARN_ACTION, 'کسب سریع')
        user = self.fresh(user)
        self.assertEqual(user.get_loyalty_level_index(), 0)
        self.assertEqual(effective_loyalty_index(user), 3)

    def test_both_histories_present_higher_of_wins(self):
        """ سنتی=۲، داینامیک=۳ -> ۳ (Higher-Of). """
        user = self.make_user_with_legacy_level(7)   # نقره‌ای = اندیس ۲
        services.credit_points(user, 2000, LoyaltyTransaction.EARN_ACTION, 'کسب سریع')
        user = self.fresh(user)
        self.assertEqual(user.get_loyalty_level_index(), 2)
        self.assertEqual(effective_loyalty_index(user), 3)

    def test_legacy_higher_than_dynamic_never_decreases(self):
        """ سنتی=۳، داینامیک=۲ -> ۳ (عدم نزول). """
        user = self.make_user_with_legacy_level(15)   # طلایی = اندیس ۳
        services.credit_points(user, 1000, LoyaltyTransaction.EARN_ACTION, 'کسب متوسط')
        user = self.fresh(user)
        self.assertEqual(user.get_loyalty_level_index(), 3)
        self.assertEqual(effective_loyalty_index(user), 3)

    def test_dynamic_tier_without_mapping_falls_back_to_legacy(self):
        """ رتبه‌ی داینامیک بدون نگاشت (legacy_equivalent_index=None) -> همان اندیس سنتی (نه ۰). """
        user = self.make_user_with_legacy_level(3)   # برنزی = اندیس ۱
        services.credit_points(user, 500, LoyaltyTransaction.EARN_ACTION, 'رسیدن به سطح بدون‌نگاشت')
        user = self.fresh(user)
        self.assertEqual(user.get_loyalty_level_index(), 1)
        self.assertEqual(effective_loyalty_index(user), 1)   # نه ۰، همان legacy


class GuestAndAnonymousWithFlagOnTests(EffectiveTierTestBase):
    """ سناریوی ۳: مهمان/ناشناس با فلگ روشن - خروجی همچنان ۰، رجیستری اصلاً فراخوانی نمی‌شود. """

    def setUp(self):
        super().setUp()
        self.set_flag(True)
        self.make_tiers()

    def test_none_user_is_zero_and_registry_is_never_called(self):
        with mock.patch('promotions.resolver.get_stat') as mocked:
            result = _loyalty_index(None)
        self.assertEqual(result, 0)
        mocked.assert_not_called()

    def test_anonymous_user_is_zero_and_registry_is_never_called(self):
        with mock.patch('promotions.resolver.get_stat') as mocked:
            result = _loyalty_index(AnonymousUser())
        self.assertEqual(result, 0)
        mocked.assert_not_called()


class CouponIntegrationTests(PromotionTestMixin, EffectiveTierTestBase):
    """
    سناریوی ۴: یکپارچه‌سازی واقعی با evaluate_coupon - کاربری با اندیس سنتی ۰ ولی رتبه‌ی
    داینامیک معادل ۲، در برابر یک کوپن با min_loyalty_level=2.
    """

    def setUp(self):
        super().setUp()
        self.category = Category.objects.create(name='دسته ۵B2', slug=f'cat-5b2-{next(_seq)}')
        self.product = Product.objects.create(
            name='کالای ۵B2', slug=f'p-5b2-{next(_seq)}', erp_code=f'ERP-5B2-{next(_seq)}',
            category=self.category, price=200000, stock=10,
        )

    def _user_with_dynamic_level_2(self):
        self.make_tiers()
        user = make_approved_user(f'0912078{next(_seq):04d}', price_level=1)   # بدون سابقه‌ی سنتی -> اندیس ۰
        services.credit_points(user, 1000, LoyaltyTransaction.EARN_ACTION, 'رسیدن به سطح معادل۲')
        return self.fresh(user)

    def _pricing_for(self, user):
        cart = Cart.objects.create(user=user)
        CartItem.objects.create(cart=cart, product=self.product, quantity=1)
        items = list(cart.items.select_related('product'))
        return price_cart(items, user, CHECK, timezone.now())

    def test_flag_off_coupon_is_rejected_as_not_eligible(self):
        self.set_flag(False)
        user = self._user_with_dynamic_level_2()
        coupon = make_coupon('EFF-TIER-OFF', min_loyalty_level=2)

        result = evaluate_coupon(coupon, user, self._pricing_for(user), now=timezone.now())

        self.assertFalse(result.ok)
        self.assertEqual(result.error, 'not_eligible')

    def test_flag_on_coupon_is_accepted(self):
        self.set_flag(True)
        user = self._user_with_dynamic_level_2()
        coupon = make_coupon('EFF-TIER-ON', min_loyalty_level=2)

        result = evaluate_coupon(coupon, user, self._pricing_for(user), now=timezone.now())

        self.assertTrue(result.ok, msg=result.message)
