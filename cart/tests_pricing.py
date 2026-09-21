"""
تست موتور قیمت‌گذاری سبد (cart/pricing.py): قیمت پایه ← تخفیف خودکار ← مبلغ نهایی، ردیف و کل، و حالت‌های مرزی
(انقضای تخفیف وسط محاسبه، روش پرداخت، کاربر ویژه، ترکیب تخفیف‌ها، یکسانی با مسیر تک‌قلم).
"""

import itertools
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from accounts.models import CustomUser
from cart.models import Cart, CartItem
from cart.pricing import CartPricing, price_cart
from products.models import Category, Product
from products.pricing import CASH, CHECK, VIP, final_price, price_breakdown
from promotions.models import DiscountPolicy
from promotions.testing import PromotionTestMixin, make_promotion

_seq = itertools.count(1)


class CartPricingBase(PromotionTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        DiscountPolicy.load()
        self.category = Category.objects.create(name='قیمت‌گذاری سبد', slug=f'cart-pricing-cat-{next(_seq)}')
        self.user = CustomUser.objects.create_user(phone_number=f'0912009{next(_seq):04d}', price_level=1)
        self.cart = Cart.objects.create(user=self.user)

    def product(self, price=100000, price2=90000, price3=80000, **extra):
        n = next(_seq)
        data = dict(name=f'کالای سبد {n}', slug=f'cart-pricing-p-{n}', erp_code=f'ERP-CP-{n}', category=self.category,
                    price=price, price2=price2, price3=price3, stock=100)
        data.update(extra)
        return Product.objects.create(**data)

    def add(self, product, quantity=1):
        return CartItem.objects.create(cart=self.cart, product=product, quantity=quantity)

    def items(self):
        return list(self.cart.items.select_related('product').order_by('pk'))

    def price(self, method=None, user=None, now=None):
        return price_cart(self.items(), user or self.user, method, now)


class BasicTests(CartPricingBase):
    def test_empty_cart(self):
        pricing = self.price()
        self.assertTrue(pricing.is_empty)
        self.assertEqual((pricing.original_total, pricing.promotion_discount, pricing.items_total, pricing.quantity),
                         (0, 0, 0, 0))
        self.assertFalse(pricing.has_discount)

    def test_no_discount_final_equals_original(self):
        self.add(self.product(), 3)
        pricing = self.price()
        line = pricing.lines[0]
        self.assertEqual((line.unit_original, line.unit_final, line.unit_discount), (100000, 100000, 0))
        self.assertEqual((line.original_total, line.discount_total, line.total), (300000, 0, 300000))
        self.assertEqual((pricing.original_total, pricing.promotion_discount, pricing.items_total), (300000, 0, 300000))
        self.assertFalse(line.has_discount)

    def test_quantity_multiplies_unit_prices(self):
        product = self.product()
        make_promotion(product, percent=20)
        self.add(product, 5)
        line = self.price().lines[0]
        self.assertEqual((line.unit_original, line.unit_discount, line.unit_final), (100000, 20000, 80000))
        self.assertEqual((line.original_total, line.discount_total, line.total), (500000, 100000, 400000))
        self.assertEqual(line.percent, 20)

    def test_mixed_cart_totals(self):
        a, b, c = self.product(), self.product(price=50000, price2=45000), self.product(price=10000, price2=9000)
        make_promotion(a, percent=10)
        make_promotion(b, kind='fixed', value=5000)
        self.add(a, 2), self.add(b, 1), self.add(c, 4)
        pricing = self.price()
        self.assertEqual([line.total for line in pricing.lines], [180000, 45000, 40000])
        self.assertEqual(pricing.original_total, 200000 + 50000 + 40000)
        self.assertEqual(pricing.promotion_discount, 20000 + 5000)
        self.assertEqual(pricing.items_total, 265000)
        self.assertEqual(pricing.original_total - pricing.promotion_discount, pricing.items_total)
        self.assertEqual(pricing.quantity, 7)
        self.assertTrue(pricing.has_discount)

    def test_order_of_lines_is_preserved(self):
        products = [self.product() for _ in range(4)]
        for product in products:
            self.add(product)
        self.assertEqual([line.product.pk for line in self.price().lines], [p.pk for p in products])

    def test_line_for_finds_the_priced_line_of_an_item(self):
        product = self.product()
        item = self.add(product, 2)
        pricing = self.price()
        self.assertEqual(pricing.line_for(item).quantity, 2)
        self.assertIsNone(pricing.line_for(CartItem(cart=self.cart, product=product)))

    def test_prices_are_whole_decimals(self):
        self.add(self.product(price=33333), 3)
        make_promotion(Product.objects.get(erp_code__startswith='ERP-CP-'), percent=33)
        line = self.price().lines[0]
        for value in (line.unit_original, line.unit_final, line.unit_discount, line.total, line.discount_total):
            self.assertEqual(value, value.to_integral_value())
            self.assertIsInstance(value, Decimal)


class PaymentMethodAndLevelTests(CartPricingBase):
    def test_method_selects_the_base_column_and_discount_applies_on_top(self):
        product = self.product()
        make_promotion(product, percent=10)
        self.add(product, 2)
        check, cash = self.price(CHECK), self.price(CASH)
        self.assertEqual((check.lines[0].unit_original, check.lines[0].unit_final), (100000, 90000))
        self.assertEqual((cash.lines[0].unit_original, cash.lines[0].unit_final), (90000, 81000))
        self.assertEqual((check.items_total, cash.items_total), (180000, 162000))
        self.assertEqual((check.method, cash.method), (CHECK, CASH))

    def test_default_method_follows_the_price_level(self):
        self.add(self.product())
        self.assertEqual(self.price().method, CHECK)                      # سطح ۱
        level2 = CustomUser.objects.create_user(phone_number=f'0912009{next(_seq):04d}', price_level=2)
        self.assertEqual(self.price(user=level2).method, CASH)

    def test_tampered_method_falls_back_to_the_default(self):
        self.add(self.product())
        pricing = self.price('FREE')
        self.assertEqual((pricing.method, pricing.items_total), (CHECK, 100000))

    def test_vip_user_is_locked_to_the_vip_price_and_gets_no_automatic_discount_by_default(self):
        product = self.product()
        make_promotion(product, percent=20)
        self.add(product, 2)
        vip = CustomUser.objects.create_user(phone_number=f'0912009{next(_seq):04d}', price_level=3)
        pricing = self.price(CHECK, user=vip)                              # تلاش برای ارزان‌تر شدن با روش دیگر
        line = pricing.lines[0]
        self.assertEqual(pricing.method, VIP)
        self.assertEqual((line.unit_original, line.unit_final, line.unit_discount), (80000, 80000, 0))
        self.assertFalse(pricing.has_discount)

    def test_vip_policy_flag_enables_the_discount_in_the_cart(self):
        product = self.product()
        make_promotion(product, percent=20)
        self.add(product)
        policy = DiscountPolicy.load()
        policy.apply_to_vip = True
        policy.save()
        vip = CustomUser.objects.create_user(phone_number=f'0912009{next(_seq):04d}', price_level=3)
        self.assertEqual(self.price(user=vip).lines[0].unit_final, 64000)

    def test_missing_cash_price_falls_back_to_the_check_price(self):
        self.add(self.product(price2=0))
        self.assertEqual(self.price(CASH).lines[0].unit_original, 100000)


class StackingAndPolicyTests(CartPricingBase):
    def test_stacked_discounts_are_itemised_and_sum_to_the_unit_discount(self):
        product = self.product()
        make_promotion(product, percent=10, priority=2, title='الف')
        make_promotion(product, percent=20, priority=1, title='ب')
        policy = DiscountPolicy.load()
        policy.promotion_stacking = 'stack'
        policy.save()
        self.add(product, 3)
        line = self.price().lines[0]
        self.assertEqual(line.unit_final, 72000)
        self.assertEqual([a.title for a in line.applied], ['الف', 'ب'])
        self.assertEqual(sum(a.discount for a in line.applied), line.unit_discount)
        self.assertEqual(line.discount_total, 28000 * 3)

    def test_global_switch_off_removes_every_discount(self):
        product = self.product()
        make_promotion(product, percent=20)
        self.add(product)
        policy = DiscountPolicy.load()
        policy.promotions_enabled = False
        policy.save()
        self.assertEqual(self.price().promotion_discount, 0)

    def test_rounding_step_is_applied_per_unit_not_per_row(self):
        product = self.product(price=33333, price2=33333)
        make_promotion(product, percent=33)
        policy = DiscountPolicy.load()
        policy.rounding_step = 100
        policy.save()
        self.add(product, 3)
        line = self.price().lines[0]
        self.assertEqual(line.unit_final % 100, 0)
        self.assertEqual(line.total, line.unit_final * 3)
        self.assertLessEqual(line.unit_final, line.unit_original)


class ServerTimeAndBoundaryTests(CartPricingBase):
    """ ساعت مرجع فقط timezone.now() سرور است؛ یک «اکنون» برای کل سبد """

    def setUp(self):
        super().setUp()
        self.a, self.b, self.c = self.product(), self.product(), self.product()
        self.promo_a = make_promotion(self.a, percent=10)
        self.promo_b = make_promotion(self.b, percent=20)
        for product in (self.a, self.b, self.c):
            self.add(product)

    def test_one_now_for_the_whole_cart(self):
        self.price()                                                       # ساخت شاخص (خواندن دیتابیس) جدا از ساعتِ قیمت‌گذاری
        with mock.patch('django.utils.timezone.now', return_value=timezone.now()) as fake_now:
            pricing = self.price()
        self.assertEqual(fake_now.call_count, 1)
        self.assertEqual(pricing.now, fake_now.return_value)

    def test_expiry_in_the_middle_of_pricing_cannot_split_the_cart(self):
        """ حتی اگر ساعتِ سرور بعد از اولین قیمت‌گذاری از انقضا رد شود، ردیف‌ها با همان «اکنون» سنجیده شده‌اند """
        end = self.promo_a.ends_at
        clock = iter([end - timedelta(milliseconds=1), end + timedelta(seconds=5), end + timedelta(seconds=9)])
        with mock.patch('django.utils.timezone.now', side_effect=lambda: next(clock)):
            pricing = self.price()
        self.assertEqual([line.has_discount for line in pricing.lines], [True, True, False])   # هر دو تخفیف هنوز فعال‌اند

    def test_promotion_boundaries_are_inclusive_at_both_ends(self):
        start, end = self.promo_a.starts_at, self.promo_a.ends_at
        cases = (
            (start - timedelta(microseconds=1), 0), (start, 1), (end - timedelta(microseconds=1), 1),
            (end, 1), (end + timedelta(microseconds=1), 0),
        )
        for now, discounted_lines in cases:
            with self.subTest(now=now.isoformat()):
                pricing = self.price(now=now)
                self.assertEqual(sum(1 for line in pricing.lines[:1] if line.has_discount), discounted_lines)

    def test_after_expiry_the_cart_total_returns_to_full_price(self):
        pricing_now = self.price()
        self.assertEqual(pricing_now.promotion_discount, 10000 + 20000)
        after = max(self.promo_a.ends_at, self.promo_b.ends_at) + timedelta(seconds=1)
        pricing_later = self.price(now=after)
        self.assertEqual((pricing_later.promotion_discount, pricing_later.items_total), (0, 300000))

    def test_scheduled_promotion_starts_by_server_time_alone(self):
        future = make_promotion(self.c, percent=50, scheduled=True)
        self.assertEqual(self.price().lines[2].unit_final, 100000)
        self.assertEqual(self.price(now=future.starts_at + timedelta(seconds=1)).lines[2].unit_final, 50000)

    def test_pricing_ignores_anything_but_server_time(self):
        """ خودِ تابع هیچ ورودیِ زمانِ کلاینت ندارد؛ نتیجه فقط تابع (سبد، کاربر، روش، اکنونِ سرور) است """
        import inspect
        self.assertEqual(list(inspect.signature(price_cart).parameters), ['items', 'user', 'method', 'now'])
        self.assertEqual(self.price().items_total, self.price().items_total)


class ConsistencyTests(CartPricingBase):
    def test_line_matches_the_single_product_path_for_every_method_and_level(self):
        product = self.product()
        make_promotion(product, percent=15)
        self.add(product, 2)
        users = [
            self.user,
            CustomUser.objects.create_user(phone_number=f'0912009{next(_seq):04d}', price_level=2),
            CustomUser.objects.create_user(phone_number=f'0912009{next(_seq):04d}', price_level=3),
            CustomUser.objects.create_user(phone_number=f'0912009{next(_seq):04d}', price_level=6),
        ]
        for user in users:
            for method in (CHECK, CASH, VIP):
                with self.subTest(level=user.price_level, method=method):
                    pricing = self.price(method, user=user)
                    single = price_breakdown(product, user, pricing.method)
                    line = pricing.lines[0]
                    self.assertEqual((line.unit_original, line.unit_final), (single.base, single.final))
                    self.assertEqual(line.unit_final, final_price(product, user, pricing.method))

    def test_invariants_over_a_matrix_of_carts(self):
        products = [self.product(price=price, price2=price - 1000) for price in (1000, 4999, 33333, 100000, 1234567)]
        for index, product in enumerate(products):
            make_promotion(product, percent=[5, 12, 33, 50, 90][index])
            self.add(product, index + 1)
        for method in (CHECK, CASH):
            pricing = self.price(method)
            for line in pricing.lines:
                self.assertEqual(line.unit_original - line.unit_discount, line.unit_final)
                self.assertGreaterEqual(line.unit_final, 0)
                self.assertLessEqual(line.unit_final, line.unit_original)
            self.assertEqual(pricing.original_total - pricing.promotion_discount, pricing.items_total)
            self.assertEqual(pricing.items_total, sum(line.total for line in pricing.lines))

    def test_cart_model_totals_use_the_engine(self):
        a, b = self.product(), self.product()
        make_promotion(a, percent=25)
        item_a, item_b = self.add(a, 2), self.add(b, 1)
        self.assertEqual(self.cart.get_total_price(), self.price().items_total)
        self.assertEqual(item_a.get_cost(), 150000)
        self.assertEqual(item_b.get_cost(), 100000)
        self.assertEqual(self.cart.get_total_price(), item_a.get_cost() + item_b.get_cost())
        self.assertIsInstance(self.cart.pricing(), CartPricing)

    def test_no_queries_once_products_and_index_are_loaded(self):
        for _ in range(5):
            product = self.product()
            make_promotion(product, percent=10)
            self.add(product)
        items = self.items()
        price_cart(items, self.user)                                       # شاخص تخفیف را گرم می‌کند
        with self.assertNumQueries(0):
            price_cart(items, self.user, CASH)
