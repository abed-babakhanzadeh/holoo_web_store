"""تست منطق مشترک سبد خرید — cart/services.py."""

import threading
from urllib.parse import parse_qs, urlparse

from django.db import IntegrityError, connections
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from accounts.models import CustomUser
from accounts.testing import make_approved_user
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

    # همه‌ی TransactionTestCaseهای پروژه باید یکسان serialized_rollback باشند؛ وگرنه بعد از flushِ یکی از آن‌ها
    # (که post_migrate را دوباره اجرا می‌کند) بازیابیِ سریال‌شده‌ی کلاس بعدی ContentType تکراری می‌سازد
    serialized_rollback = True

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


class CartActionLoginRedirectTests(TestCase):
    """
    فاز ۴: AddToCartView/DecreaseCartView/RemoveFromCartView فقط POST دارند. اگر مهمان مستقیماً (بدون UI،
    دکمه‌ها فقط برای کاربر واردشده رندر می‌شوند) این آدرس‌ها را صدا بزند، LoginRequiredMixin پیش‌فرض او را به
    login?next=همین‌آدرس می‌فرستاد؛ چون آن آدرس فقط POST جواب می‌دهد، بعد از ورود یک GET رویش ۴۰۵ می‌داد.
    """

    @classmethod
    def setUpTestData(cls):
        cls.product = _make_product()
        cls.user = make_approved_user('09121234099', price_level=1)

    @staticmethod
    def _next_param(response):
        return parse_qs(urlparse(response['Location']).query)['next'][0]

    def test_guest_add_to_cart_redirects_to_the_product_detail_page_not_the_action_url(self):
        response = self.client.post(reverse('cart:add_to_cart', args=[self.product.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._next_param(response), reverse('products:product_detail', args=[self.product.slug]))

    def test_the_redirect_target_is_actually_get_able_after_login(self):
        """ قبلاً همین سناریو (افزودن مستقیم ← ورود ← دنبال‌کردن next) روی آدرس POST-فقط با ۴۰۵ شکست می‌خورد """
        response = self.client.post(reverse('cart:add_to_cart', args=[self.product.pk]))
        next_url = self._next_param(response)
        self.client.force_login(self.user)
        follow_up = self.client.get(next_url)
        self.assertEqual(follow_up.status_code, 200)

    def test_decrease_and_remove_also_redirect_to_a_get_able_page(self):
        for url_name in ('cart:decrease_cart', 'cart:remove_from_cart'):
            with self.subTest(url_name=url_name):
                response = self.client.post(reverse(url_name, args=[self.product.pk]))
                self.assertEqual(response.status_code, 302)
                self.assertEqual(self._next_param(response), reverse('products:product_detail', args=[self.product.slug]))

    def test_unknown_product_id_falls_back_to_home_without_crashing(self):
        response = self.client.post(reverse('cart:add_to_cart', args=[999999]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._next_param(response), reverse('products:home'))

    def test_authenticated_user_is_unaffected_and_gets_a_normal_response(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse('cart:add_to_cart', args=[self.product.pk]))
        self.assertEqual(response.status_code, 200)


class CartActionApprovalGateTests(TestCase):
    """
    فاز ۲/۳: گیت Fail-Closed can_order() روی اکشن‌های سبد. کاربرِ تأییدنشده (PENDING/REJECTED) نباید
    بتواند مستقیماً (با دستکاری URL؛ دکمه‌ها فقط برای کاربر مجاز رندر می‌شوند) به سبد اضافه/کم/حذف کند —
    نه خطای ۵۰۰ یا عدد نامعتبر (چون price_breakdown برای او None برمی‌گرداند)، بلکه یک ریدایرکت ساده،
    و مهم‌تر: هیچ ردیف سبدی هم نباید واقعاً نوشته/تغییر کند.
    """

    @classmethod
    def setUpTestData(cls):
        cls.product = _make_product()

    def setUp(self):
        self.pending = CustomUser.objects.create_user(
            phone_number='09150003001', first_name='ه', last_name='و', national_code='1231231241',
        )
        self.rejected = CustomUser.objects.create_user(
            phone_number='09150003002', first_name='ز', last_name='ح', national_code='1231231242',
        )
        self.rejected.reject(reason='تست')

    def test_pending_user_cannot_add_to_cart(self):
        self.client.force_login(self.pending)
        response = self.client.post(reverse('cart:add_to_cart', args=[self.product.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(CartItem.objects.filter(cart__user=self.pending).exists())

    def test_rejected_user_cannot_add_to_cart(self):
        self.client.force_login(self.rejected)
        response = self.client.post(reverse('cart:add_to_cart', args=[self.product.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(CartItem.objects.filter(cart__user=self.rejected).exists())

    def test_pending_user_cannot_decrease_or_remove(self):
        for url_name in ('cart:decrease_cart', 'cart:remove_from_cart'):
            with self.subTest(url_name=url_name):
                self.client.force_login(self.pending)
                response = self.client.post(reverse(url_name, args=[self.product.pk]))
                self.assertEqual(response.status_code, 302)

    def test_mini_cart_and_nav_cart_do_not_crash_for_a_pending_user_with_a_leftover_cart_item(self):
        """
        دفاع در عمق: اگر به هر دلیلی (مثلاً داده‌ی قدیمیِ قبل از این فیچر) کاربرِ تأییدنشده از قبل
        ردیف سبد داشته باشد، صفحات نمایشی سبد نباید با None از پرداخت‌گذاری بشکنند — فقط چیزی نشان
        نمی‌دهند (cart=None در context)، نه خطای ۵۰۰.
        """
        cart = Cart.objects.create(user=self.pending)
        CartItem.objects.create(cart=cart, product=self.product, quantity=1)
        self.client.force_login(self.pending)
        for url_name in ('cart:mini_cart', 'cart:nav_cart'):
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(url_name))
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, '100000')
