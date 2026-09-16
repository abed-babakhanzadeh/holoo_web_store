"""تست قیمت‌گذاری — تنها منبع حقیقت قیمت در کل پروژه."""

from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from accounts.models import CustomUser
from products.models import Category, Discount, Product
from products.pricing import (
    CASH, CHECK, VIP, base_price, default_payment_method, final_price, resolve_payment_method,
)


class PricingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.category = Category.objects.create(name='تست', slug='test-cat')
        cls.product = Product.objects.create(
            name='کالای تست', slug='test-product', erp_code='ERP-TEST-1',
            category=cls.category, price=100000, price2=90000, price3=80000, stock=10,
        )
        cls.level1 = CustomUser.objects.create_user(phone_number='09120000001', price_level=1)
        cls.level2 = CustomUser.objects.create_user(phone_number='09120000002', price_level=2)
        cls.level3 = CustomUser.objects.create_user(phone_number='09120000003', price_level=3)

    # --- نگاشت روش پرداخت به ستون قیمت ---

    def test_payment_method_maps_to_correct_price_column(self):
        self.assertEqual(base_price(self.product, self.level1, CHECK), Decimal('100000'))
        self.assertEqual(base_price(self.product, self.level1, CASH), Decimal('90000'))
        self.assertEqual(base_price(self.product, self.level3, VIP), Decimal('80000'))

    def test_default_method_follows_user_price_level(self):
        self.assertEqual(default_payment_method(self.level1), CHECK)
        self.assertEqual(default_payment_method(self.level2), CASH)
        self.assertEqual(default_payment_method(self.level3), VIP)

    def test_zero_tier_price_falls_back_to_price_one(self):
        """ اگر قیمت آن سطح در هلو پر نشده باشد نباید فاکتور با مبلغ صفر ثبت شود """
        self.product.price2 = 0
        self.assertEqual(base_price(self.product, self.level1, CASH), Decimal('100000'))

    # --- امنیت انتخاب روش پرداخت ---

    def test_vip_user_is_locked_to_vip_even_if_form_says_otherwise(self):
        self.assertEqual(resolve_payment_method(self.level3, CHECK), VIP)

    def test_price_level_above_three_is_also_vip(self):
        """ سطح ۴ تا ۱۰ هم ویژه‌اند؛ قبلاً سبد priceN می‌داد ولی فاکتور price1 ثبت می‌کرد """
        user = CustomUser.objects.create_user(phone_number='09120000005', price_level=5)
        self.assertEqual(resolve_payment_method(user, CHECK), VIP)

    def test_invalid_method_falls_back_to_user_default(self):
        self.assertEqual(resolve_payment_method(self.level1, 'FREE'), CHECK)
        self.assertEqual(resolve_payment_method(self.level2, ''), CASH)

    # --- تخفیف ---

    def _add_discount(self, percent=25, active=True, expired=False):
        now = timezone.now()
        return Discount.objects.create(
            product=self.product, percent=percent, is_active=active,
            starts_at=now - timedelta(days=2),
            ends_at=now - timedelta(days=1) if expired else now + timedelta(days=1),
        )

    def test_active_discount_is_applied(self):
        self._add_discount(25)
        self.assertEqual(final_price(self.product, self.level1, CHECK), Decimal('75000'))

    def test_expired_discount_is_ignored(self):
        self._add_discount(25, expired=True)
        self.assertEqual(final_price(self.product, self.level1, CHECK), Decimal('100000'))

    def test_inactive_discount_is_ignored(self):
        self._add_discount(25, active=False)
        self.assertEqual(final_price(self.product, self.level1, CHECK), Decimal('100000'))

    def test_best_discount_wins(self):
        self._add_discount(10)
        self._add_discount(40)
        self.assertEqual(final_price(self.product, self.level1, CHECK), Decimal('60000'))

    def test_discount_applies_on_top_of_payment_method_price(self):
        self._add_discount(50)
        self.assertEqual(final_price(self.product, self.level1, CASH), Decimal('45000'))

    def test_price_is_rounded_to_whole_toman(self):
        """ عدد نمایش‌داده‌شده و عدد ذخیره‌شده در OrderItem باید دقیقاً یکی باشند """
        self.product.price = 33333
        self._add_discount(33)
        self.assertEqual(final_price(self.product, self.level1, CHECK), Decimal('22333'))

    # --- هماهنگی کارت / سبد / فاکتور ---

    def test_card_cart_and_invoice_agree(self):
        from cart.models import Cart, CartItem
        self._add_discount(20)

        card = self.product.get_discounted_price(self.level1)
        invoice = final_price(self.product, self.level1, resolve_payment_method(self.level1, CHECK))

        cart = Cart.objects.create(user=self.level1)
        item = CartItem.objects.create(cart=cart, product=self.product, quantity=3)

        self.assertEqual(card, invoice)
        self.assertEqual(item.get_cost(), card * 3)

    def test_anonymous_user_gets_price_one(self):
        self.assertEqual(base_price(self.product, None), Decimal('100000'))
