"""
تست‌های Loyalty Phase 5A-2: نقطه‌اتصال انتزاعی سطح وفاداری مؤثر
(loyalty/stats.py::effective_loyalty_index + promotions/resolver.py::_loyalty_index).

هدف این فاز صرفاً ساخت یک Seam معماری بود - این تست‌ها اثبات می‌کنند که این Seam هیچ اثر
مشاهده‌پذیری روی رفتار سنتی ندارد: خروجی همیشه دقیقاً همان user.get_loyalty_level_index() است،
صرف‌نظر از این‌که کاربر LoyaltyAccount/lifetime_earned چه مقداری دارد.
"""

import itertools
from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from accounts.models import CustomUser
from accounts.stats import get as get_stat
from accounts.testing import make_approved_user
from cart.models import Cart, CartItem
from cart.pricing import price_cart
from loyalty import services
from loyalty.models import LoyaltyTransaction
from loyalty.stats import effective_loyalty_index
from orders.models import Order, OrderItem
from payments.models import Transaction
from products.models import Category, Product, SiteSettings
from products.pricing import CHECK
from promotions.coupons import evaluate_coupon
from promotions.resolver import _loyalty_index
from promotions.testing import PromotionTestMixin, make_coupon

_seq = itertools.count(1)


def _make_paid_order(user, *, item_price=None, item_qty=1):
    """ یک سفارش با تراکنش پرداخت موفق - عیناً همان کمک‌تابع accounts/tests_loyalty.py """
    items_total = (item_price or 0) * item_qty
    order = Order.objects.create(
        user=user, first_name='کاربر', last_name='تست', phone=user.phone_number,
        address='تهران', payment_method='cash', total_price=items_total,
    )
    if item_price is not None:
        OrderItem.objects.create(order=order, price=item_price, quantity=item_qty)
    Transaction.objects.create(
        user=user, order=order, amount=order.total_price,
        authority=f'TEST-5A2-{next(_seq)}', status='success',
    )
    return order


class LoyaltyStatsTestBase(TestCase):
    """ هم‌الگوی accounts/tests_loyalty.py::LoyaltyTestBase - تنظیمات سطح سنتی را دترمینیستیک می‌کند. """

    def setUp(self):
        super().setUp()
        cache.delete(SiteSettings.CACHE_KEY)
        SiteSettings.load().save()
        self.addCleanup(self._reset_settings)

    def _reset_settings(self):
        obj = SiteSettings.load()
        obj.loyalty_mode = SiteSettings.LOYALTY_MODE_ORDER_COUNT
        obj.loyalty_points_per_order = 100
        obj.loyalty_threshold_bronze = 300
        obj.loyalty_threshold_silver = 700
        obj.loyalty_threshold_gold = 1500
        obj.loyalty_threshold_diamond = 3000
        obj.loyalty_activated_at = None
        obj.save()
        cache.delete(SiteSettings.CACHE_KEY)

    def fresh(self, user):
        """ نمونه‌ی تازه بدون cached_propertyهای باقی‌مانده (paid_orders_count/_loyalty_config) """
        return CustomUser.objects.get(pk=user.pk)

    def make_user_with_level(self, paid_orders):
        user = CustomUser.objects.create_user(phone_number=f'0912075{next(_seq):04d}')
        for _ in range(paid_orders):
            _make_paid_order(user)
        return self.fresh(user)


class EffectiveIndexMatchesLegacyTests(LoyaltyStatsTestBase):
    """ سناریوی ۱: برای هر ۵ سطح سنتی (۰ تا ۴)، effective_loyalty_index دقیقاً برابر legacy است. """

    def test_matches_legacy_for_every_level(self):
        # آستانه‌ها: برنزی=۳۰۰(۳سفارش)، نقره‌ای=۷۰۰(۷سفارش)، طلایی=۱۵۰۰(۱۵سفارش)، الماسی=۳۰۰۰(۳۰سفارش)
        cases = [(0, 0), (3, 1), (7, 2), (15, 3), (30, 4)]
        for paid_orders, expected_index in cases:
            with self.subTest(paid_orders=paid_orders):
                user = self.make_user_with_level(paid_orders)
                self.assertEqual(user.get_loyalty_level_index(), expected_index)
                self.assertEqual(effective_loyalty_index(user), expected_index)
                self.assertEqual(effective_loyalty_index(user), user.get_loyalty_level_index())


class LoyaltyAccountHasNoEffectTests(LoyaltyStatsTestBase):
    """ سناریوی ۲: LoyaltyAccount/lifetime_earned کاملاً بی‌اثر است - هرچقدر هم بزرگ باشد. """

    def test_lifetime_earned_does_not_change_the_effective_index(self):
        user = self.make_user_with_level(7)   # سطح سنتی: نقره‌ای (۲)
        legacy_index = user.get_loyalty_level_index()

        # مقدار عمداً بزرگ و بی‌ربط به آستانه‌های سنتی
        services.credit_points(user, 50000, LoyaltyTransaction.EARN_ACTION, 'تست بی‌اثری لجر')
        user = self.fresh(user)

        self.assertEqual(effective_loyalty_index(user), legacy_index)
        self.assertEqual(effective_loyalty_index(user), 2)

    def test_zero_lifetime_earned_also_matches_legacy(self):
        user = self.make_user_with_level(3)   # سطح سنتی: برنزی (۱)، بدون هیچ LoyaltyAccount ای
        self.assertEqual(effective_loyalty_index(user), 1)
        self.assertEqual(effective_loyalty_index(user), user.get_loyalty_level_index())


class RegistryIntegrationTests(LoyaltyStatsTestBase):
    """ سناریوی ۳: فراخوانی از طریق رجیستری accounts.stats. """

    def test_get_stat_returns_the_provider_value(self):
        user = self.make_user_with_level(7)
        result = get_stat('effective_loyalty_index', user, default=999)
        self.assertEqual(result, 2)
        self.assertNotEqual(result, 999)

    def test_get_stat_falls_back_when_provider_is_not_registered(self):
        user = self.make_user_with_level(7)
        with mock.patch('accounts.stats._providers', {}):
            result = get_stat('effective_loyalty_index', user, default=123)
        self.assertEqual(result, 123)


class GuestAndAnonymousTests(LoyaltyStatsTestBase):
    """ سناریوی ۴: مهمان/ناشناس - همیشه صفر، بدون فراخوانی رجیستری. """

    def test_loyalty_index_is_zero_for_none_and_registry_is_never_called(self):
        with mock.patch('promotions.resolver.get_stat') as mocked:
            result = _loyalty_index(None)
        self.assertEqual(result, 0)
        mocked.assert_not_called()

    def test_loyalty_index_is_zero_for_anonymous_user_and_registry_is_never_called(self):
        with mock.patch('promotions.resolver.get_stat') as mocked:
            result = _loyalty_index(AnonymousUser())
        self.assertEqual(result, 0)
        mocked.assert_not_called()


class TransitionRegressionTests(PromotionTestMixin, LoyaltyStatsTestBase):
    """
    سناریوی ۵ (حیاتی‌ترین): مشتری‌ای با سابقه‌ی سفارش سنتی اما lifetime_earned=0 - واجدشرایط‌بودنش
    باید دقیقاً طبق سیستم سنتی ارزیابی شود، بدون هیچ اثری از سمت داینامیک (که اینجا صفر است).
    """

    def setUp(self):
        super().setUp()
        self.category = Category.objects.create(name='دسته ۵A2', slug=f'cat-5a2-{next(_seq)}')
        self.product = Product.objects.create(
            name='کالای ۵A2', slug=f'p-5a2-{next(_seq)}', erp_code=f'ERP-5A2-{next(_seq)}',
            category=self.category, price=200000, stock=10,
        )

    def make_priced_user_with_level(self, paid_orders):
        """ هم‌الگوی promotions/tests_coupons.py::CouponBase.new_user - کاربر تأییدشده با سطح
        قیمت ۱، تا موتور واقعی قیمت‌گذاری سبد (cart.pricing.price_cart) بتواند قیمت بدهد. """
        user = make_approved_user(f'0912076{next(_seq):04d}', price_level=1)
        for _ in range(paid_orders):
            _make_paid_order(user)
        return self.fresh(user)

    def _pricing_for(self, user):
        cart = Cart.objects.create(user=user)
        CartItem.objects.create(cart=cart, product=self.product, quantity=1)
        items = list(cart.items.select_related('product'))
        return price_cart(items, user, CHECK, timezone.now())

    def test_customer_with_legacy_history_and_zero_dynamic_points_keeps_legacy_eligibility(self):
        user = self.make_priced_user_with_level(7)   # سطح سنتی: نقره‌ای (۲)، بدون LoyaltyAccount (lifetime_earned عملاً صفر)
        coupon = make_coupon('TRANS-REG-1', min_loyalty_level=2)   # نیازمند «نقره‌ای و بالاتر»

        result = evaluate_coupon(coupon, user, self._pricing_for(user), now=timezone.now())

        self.assertTrue(result.ok, msg=result.message)

    def test_customer_below_legacy_threshold_is_still_correctly_rejected(self):
        """ اثبات این‌که Seam فقط «شفاف» است، نه این‌که همیشه واجد شرایط را True کند. """
        user = self.make_priced_user_with_level(1)   # سطح سنتی: مشتری جدید (۰) - زیر آستانه‌ی نقره‌ای
        coupon = make_coupon('TRANS-REG-2', min_loyalty_level=2)

        result = evaluate_coupon(coupon, user, self._pricing_for(user), now=timezone.now())

        self.assertFalse(result.ok)
        self.assertEqual(result.error, 'not_eligible')
