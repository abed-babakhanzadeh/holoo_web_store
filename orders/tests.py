"""تست ثبت سفارش: اعتبارسنجی ورودی، قفل‌شدن قیمت، و انتشار رویداد."""

from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomUser
from cart.models import Cart, CartItem
from orders.forms import CheckoutForm
from orders.models import Order
from products.models import Category, Discount, Product


class CheckoutFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # کاربر بدون آدرس/نام، تا fallback پروفایل نتیجه‌ی تست را عوض نکند
        cls.user = CustomUser.objects.create_user(phone_number='09120000020')

    def _form(self, **overrides):
        data = {'first_name': 'علی', 'last_name': 'رضایی', 'phone': '09121112233',
                'address': 'تهران، خیابان آزادی', 'postal_code': '1234567890'}
        data.update(overrides)
        return CheckoutForm(data, user=self.user)

    def test_valid_data_passes(self):
        self.assertTrue(self._form().is_valid())

    def test_blank_address_is_rejected(self):
        self.assertFalse(self._form(address='   ').is_valid())

    def test_invalid_phone_is_rejected(self):
        self.assertFalse(self._form(phone='123').is_valid())

    def test_short_postal_code_is_rejected(self):
        self.assertFalse(self._form(postal_code='12345').is_valid())

    def test_postal_code_is_optional(self):
        self.assertTrue(self._form(postal_code='').is_valid())

    def test_persian_digits_in_phone_are_normalized(self):
        form = self._form(phone='۰۹۱۲۱۱۱۲۲۳۳')
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data['phone'], '09121112233')

    def test_tampered_payment_method_is_discarded(self):
        form = self._form(payment_method='FREE')
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data['payment_method'], '')

    def test_missing_fields_fall_back_to_profile(self):
        self.user.address = 'آدرس پروفایل'
        self.user.first_name = 'رضا'
        form = self._form(address='', first_name='')
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data['address'], 'آدرس پروفایل')
        self.assertEqual(form.cleaned_data['first_name'], 'رضا')


class SubmitOrderTests(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(phone_number='09120000021', price_level=1)
        category = Category.objects.create(name='تست', slug='order-test-cat')
        self.product = Product.objects.create(
            name='کالا', slug='order-test-product', erp_code='ERP-ORDER-1',
            category=category, price=100000, price2=90000, stock=10,
        )
        self.cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(cart=self.cart, product=self.product, quantity=2)
        self.client.force_login(self.user)

    VALID = {'first_name': 'علی', 'last_name': 'رضایی', 'phone': '09121112233',
             'address': 'تهران', 'postal_code': '1234567890', 'payment_method': 'check'}

    def _submit(self, **overrides):
        data = dict(self.VALID)
        data.update(overrides)
        # رویداد order_placed داخل on_commit منتشر می‌شود و در TestCase (که کل تست را در
        # یک تراکنش rollback‌شونده می‌پیچد) به‌خودی‌خود اجرا نمی‌شود
        with mock.patch('holoo.receivers.send_order_to_holoo') as task:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(reverse('orders:submit_order'), data)
        return response, task

    def test_order_is_created_with_frozen_prices(self):
        self._submit()
        order = Order.objects.get(user=self.user)
        item = order.items.get()

        self.assertEqual(item.price, Decimal('100000'))
        self.assertEqual(item.quantity, 2)
        self.assertEqual(order.total_price, Decimal('200000') + order.shipping_cost)

    def test_invoice_total_equals_sum_of_rows(self):
        self._submit()
        order = Order.objects.get(user=self.user)
        rows = sum(i.price * i.quantity for i in order.items.all())
        self.assertEqual(order.total_price, rows + order.shipping_cost)

    def test_active_discount_is_charged(self):
        """ باگ اصلی: تخفیف روی کارت نمایش داده می‌شد ولی در فاکتور اعمال نمی‌شد """
        now = timezone.now()
        Discount.objects.create(product=self.product, percent=20, is_active=True,
                                starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=1))
        self._submit()
        self.assertEqual(Order.objects.get(user=self.user).items.get().price, Decimal('80000'))

    def test_payment_method_changes_charged_price(self):
        self._submit(payment_method='cash')
        self.assertEqual(Order.objects.get(user=self.user).items.get().price, Decimal('90000'))

    def test_cart_is_emptied_after_submit(self):
        self._submit()
        self.assertFalse(Cart.objects.filter(user=self.user).exists())

    def test_order_placed_signal_reaches_accounting(self):
        _, task = self._submit()
        order = Order.objects.get(user=self.user)
        task.delay.assert_called_once_with(order.id)

    def test_invalid_data_creates_no_order_and_keeps_cart(self):
        response, task = self._submit(phone='نامعتبر', address='')

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Order.objects.filter(user=self.user).exists())
        self.assertTrue(Cart.objects.filter(user=self.user).exists())
        self.assertEqual(task.delay.call_count, 0)

    def test_vip_user_cannot_downgrade_to_cheaper_method(self):
        self.user.price_level = 3
        self.user.save(update_fields=['price_level'])
        self.product.price3 = 70000
        self.product.save(update_fields=['price3'])

        self._submit(payment_method='check')
        order = Order.objects.get(user=self.user)
        self.assertEqual(order.payment_method, 'vip')
        self.assertEqual(order.items.get().price, Decimal('70000'))
