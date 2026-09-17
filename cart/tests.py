"""تست منطق مشترک سبد خرید — cart/services.py."""

import threading

from django.db import IntegrityError, connections
from django.test import TestCase, TransactionTestCase

from accounts.models import CustomUser
from products.models import Category, Product, ProductColor

from .models import Cart, CartItem
from .services import add_item, decrease_item


def _make_product(stock=10, **extra):
    category = Category.objects.create(name='دسته تست', slug=f'cat-{Category.objects.count()}')
    return Product.objects.create(
        name='کالای تست سبد', slug=f'cart-test-{Product.objects.count()}',
        erp_code=f'ERP-CART-{Product.objects.count()}',
        category=category, price=100000, stock=stock, **extra,
    )


class AddItemTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = CustomUser.objects.create_user(phone_number='09120000010')
        cls.cart = Cart.objects.create(user=cls.user)

    def test_creates_new_row_when_in_stock(self):
        product = _make_product(stock=5)
        item = add_item(self.cart, product)
        self.assertIsNotNone(item)
        self.assertEqual(item.quantity, 1)
        self.assertEqual(CartItem.objects.filter(cart=self.cart, product=product).count(), 1)

    def test_does_nothing_when_out_of_stock_and_no_existing_row(self):
        product = _make_product(stock=0)
        item = add_item(self.cart, product)
        self.assertIsNone(item)
        self.assertFalse(CartItem.objects.filter(cart=self.cart, product=product).exists())

    def test_increments_existing_row(self):
        product = _make_product(stock=5)
        add_item(self.cart, product)
        item = add_item(self.cart, product)
        self.assertEqual(item.quantity, 2)

    def test_does_not_exceed_stock(self):
        product = _make_product(stock=2)
        add_item(self.cart, product)
        add_item(self.cart, product)
        item = add_item(self.cart, product)  # سومین تلاش، موجودی فقط ۲ است
        self.assertEqual(item.quantity, 2)

    def test_each_color_gets_its_own_row(self):
        product = _make_product(stock=5)
        red = ProductColor.objects.create(product=product, name='قرمز')
        blue = ProductColor.objects.create(product=product, name='آبی')

        item_red = add_item(self.cart, product, color_id=red.id)
        item_blue = add_item(self.cart, product, color_id=blue.id)

        self.assertNotEqual(item_red.pk, item_blue.pk)
        self.assertEqual(CartItem.objects.filter(cart=self.cart, product=product).count(), 2)


class DecreaseItemTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = CustomUser.objects.create_user(phone_number='09120000011')
        cls.cart = Cart.objects.create(user=cls.user)

    def test_decrements_quantity_above_one(self):
        product = _make_product(stock=5)
        add_item(self.cart, product)
        add_item(self.cart, product)
        item = decrease_item(self.cart, product)
        self.assertEqual(item.quantity, 1)

    def test_deletes_row_when_quantity_reaches_zero(self):
        product = _make_product(stock=5)
        add_item(self.cart, product)
        item = decrease_item(self.cart, product)
        self.assertIsNone(item)
        self.assertFalse(CartItem.objects.filter(cart=self.cart, product=product).exists())

    def test_no_op_when_row_does_not_exist(self):
        product = _make_product(stock=5)
        item = decrease_item(self.cart, product)
        self.assertIsNone(item)


class CartRaceConditionTests(TransactionTestCase):
    """
    اثبات عملی رفع دو باگ همزمانی که قبل از cart/services.py وجود داشت:
    دابل‌کلیک روی «افزودن» دیگر IntegrityError نمی‌دهد و quantity++ همزمان گم نمی‌شود.
    باید TransactionTestCase باشد نه TestCase، چون هر Thread باید تراکنش واقعی خودش را
    commit کند تا Thread دیگر قفل select_for_update را واقعاً حس کند.
    """

    def setUp(self):
        self.user = CustomUser.objects.create_user(phone_number='09120000012')
        self.cart = Cart.objects.create(user=self.user)
        self.product = _make_product(stock=100)

    def _run_concurrently(self, target, count):
        errors = []

        def worker():
            try:
                target()
            except Exception as exc:  # noqa: BLE001 - می‌خواهیم هر خطایی از race را ببینیم
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker) for _ in range(count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return errors

    def test_concurrent_double_click_does_not_raise_integrity_error(self):
        errors = self._run_concurrently(lambda: add_item(self.cart, self.product), count=8)
        self.assertEqual(errors, [], f"IntegrityError یا خطای دیگری در حین افزودن هم‌زمان رخ داد: {errors}")

        item = CartItem.objects.get(cart=self.cart, product=self.product)
        self.assertEqual(item.quantity, 8, "quantity++ همزمان باید بدون lost update دقیقاً به تعداد ریکوئست‌ها برسد")
