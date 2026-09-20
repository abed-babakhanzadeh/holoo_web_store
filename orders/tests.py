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

    def test_missing_name_falls_back_to_profile_but_address_is_still_required(self):
        # کاربر بدون آدرس ثبت‌شده: نام از پروفایل پر می‌شود ولی آدرس همچنان الزامی است
        user = CustomUser.objects.create_user(phone_number='09120000029', first_name='رضا', last_name='احمدی')
        form = CheckoutForm({'address': '', 'first_name': '', 'last_name': '', 'phone': ''}, user=user)
        self.assertFalse(form.is_valid())
        self.assertEqual(list(form.errors), ['address'])
        self.assertEqual(form.profile_defaults['first_name'], 'رضا')
        self.assertEqual(form.profile_defaults['phone'], '09120000029')

    def test_empty_fields_fall_back_to_the_default_address(self):
        from accounts.models import Address
        from locations.models import City, Province
        user = CustomUser.objects.create_user(phone_number='09120000028')
        city = City.objects.create(province=Province.objects.create(name='استان تست تسویه'), name='شهر تست تسویه')
        Address.objects.create(user=user, title='منزل', receiver_first_name='مریم', receiver_last_name='کاظمی',
                               receiver_phone='09123334455', city=city, postal_code='1112223334', address='بلوار تست، پلاک ۵')
        other = Address.objects.create(user=user, title='محل کار', receiver_first_name='دیگری', receiver_last_name='دیگری',
                                       receiver_phone='09120000000', city=city, postal_code='9999999999', address='جای دیگر')
        self.assertFalse(other.is_default)

        form = CheckoutForm({'address': '', 'first_name': '', 'last_name': '', 'phone': '', 'postal_code': ''}, user=user)

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['first_name'], 'مریم')
        self.assertEqual(form.cleaned_data['last_name'], 'کاظمی')
        self.assertEqual(form.cleaned_data['phone'], '09123334455')
        self.assertEqual(form.cleaned_data['postal_code'], '1112223334')
        self.assertEqual(form.cleaned_data['address'], 'استان تست تسویه، شهر تست تسویه، بلوار تست، پلاک ۵')

    def test_explicit_values_win_over_the_default_address(self):
        from accounts.models import Address
        from locations.models import City, Province
        user = CustomUser.objects.create_user(phone_number='09120000027')
        city = City.objects.create(province=Province.objects.create(name='استان تست تسویه ۲'), name='شهر تست تسویه ۲')
        Address.objects.create(user=user, title='منزل', receiver_first_name='مریم', receiver_last_name='کاظمی',
                               receiver_phone='09123334455', city=city, postal_code='1112223334', address='بلوار تست')
        form = CheckoutForm({'address': 'آدرس دستی', 'first_name': 'علی', 'last_name': 'رضایی',
                             'phone': '09121112233', 'postal_code': '5556667778'}, user=user)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['address'], 'آدرس دستی')
        self.assertEqual(form.cleaned_data['first_name'], 'علی')


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

    def test_shipping_cost_comes_from_site_settings_not_hardcoded(self):
        """ هزینه ارسال دیگر ثابت SHIPPING_COST نیست؛ از تنظیمات سایت (قابل‌تغییر در ادمین) خوانده می‌شود """
        from django.core.cache import cache
        from products.models import SiteSettings
        self.addCleanup(cache.delete, SiteSettings.CACHE_KEY)  # کش Redis با rollback تراکنش تست پاک نمی‌شود

        settings_obj = SiteSettings.load()
        settings_obj.shipping_cost = 55000
        settings_obj.save()

        self._submit()
        order = Order.objects.get(user=self.user)
        self.assertEqual(order.shipping_cost, 55000)
        self.assertEqual(order.total_price, Decimal('200000') + 55000)

    def test_free_shipping_waived_only_when_every_item_qualifies(self):
        """
        هزینه ارسال به‌ازای کل مرسوله است، نه هر کالا؛ پس با وجود حتی یک کالای غیر
        ارسال‌رایگان در سبد، باز هم باید هزینه‌ی کامل ارسال گرفته شود.
        """
        other = Product.objects.create(
            name='کالای دوم', slug='order-test-product-2', erp_code='ERP-ORDER-2',
            category=self.product.category, price=50000, stock=10, free_shipping=False,
        )
        CartItem.objects.create(cart=self.cart, product=other, quantity=1)
        self.product.free_shipping = True
        self.product.save(update_fields=['free_shipping'])

        self._submit()
        order = Order.objects.get(user=self.user)
        self.assertEqual(order.shipping_cost, 200000)

    def test_free_shipping_waived_when_all_items_qualify(self):
        self.product.free_shipping = True
        self.product.save(update_fields=['free_shipping'])

        self._submit()
        order = Order.objects.get(user=self.user)
        self.assertEqual(order.shipping_cost, 0)
        self.assertEqual(order.total_price, Decimal('200000'))

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

    def test_order_placed_notifies_customer(self):
        """ order_placed یک شنونده‌ی مستقل دیگر هم دارد: تایید سفارش برای مشتری """
        self.user.first_name = 'علی'
        self.user.save(update_fields=['first_name'])

        with mock.patch('notifications.receivers.notify') as notify_mock:
            self._submit()
        order = Order.objects.get(user=self.user)

        notify_mock.assert_called_once_with(self.user.phone_number, 'order_placed_customer', name='علی', order_id=order.id)

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


class OrderAdminShippedNotificationTests(TestCase):
    """
    OrderAdmin.save_model باید پیامک کد رهگیری را دقیقاً وقتی بفرستد که در همان ذخیره
    وضعیت به «ارسال شده» تغییر کرده یا کد رهگیری تازه پر شده — نه در هر ذخیره‌ی بعدی
    که به این دو فیلد کاری ندارد (تا با هر تغییر کوچک دیگر پیامک تکراری نرود).
    """

    def setUp(self):
        from django.contrib.admin.sites import AdminSite
        from orders.admin import OrderAdmin

        self.user = CustomUser.objects.create_user(phone_number='09120000022', first_name='رضا')
        category = Category.objects.create(name='تست', slug='admin-ship-test-cat')
        product = Product.objects.create(
            name='کالا', slug='admin-ship-test-product', erp_code='ERP-ADMIN-SHIP-1',
            category=category, price=100000, stock=5,
        )
        self.order = Order.objects.create(
            user=self.user, first_name='رضا', last_name='ی', phone='09120000022',
            address='تهران', payment_method='cash', shipping_cost=0, total_price=100000, status='processing',
        )
        self.admin = OrderAdmin(Order, AdminSite())

    def _save(self, obj, changed_data):
        form = mock.Mock(changed_data=changed_data)
        self.admin.save_model(request=mock.Mock(), obj=obj, form=form, change=True)

    def test_notifies_when_status_becomes_shipped_with_tracking_code(self):
        self.order.status = 'shipped'
        self.order.tracking_code = 'POST-123'
        with mock.patch('notifications.service.notify') as notify_mock:
            self._save(self.order, changed_data=['status', 'tracking_code'])

        notify_mock.assert_called_once_with(
            '09120000022', 'order_shipped_customer', name='رضا', tracking_code='POST-123',
        )

    def test_no_notification_when_tracking_code_still_missing(self):
        self.order.status = 'shipped'
        with mock.patch('notifications.service.notify') as notify_mock:
            self._save(self.order, changed_data=['status'])
        self.assertEqual(notify_mock.call_count, 0)

    def test_no_duplicate_notification_on_unrelated_resave(self):
        """ یک ذخیره‌ی بعدی که به وضعیت/کد رهگیری کاری ندارد نباید دوباره پیامک بفرستد """
        self.order.status = 'shipped'
        self.order.tracking_code = 'POST-123'
        self.order.save()

        with mock.patch('notifications.service.notify') as notify_mock:
            self._save(self.order, changed_data=['address'])
        self.assertEqual(notify_mock.call_count, 0)


class CheckoutPageAddressPrefillTests(TestCase):
    """ صفحه‌ی تسویه‌حساب فیلدهای گیرنده را از آدرس پیش‌فرض کاربر پر می‌کند (و بدون آدرس، از پروفایل) """

    def setUp(self):
        self.user = CustomUser.objects.create_user(phone_number='09120000031', first_name='علی', last_name='رضایی')
        category = Category.objects.create(name='تست', slug='prefill-test-cat')
        product = Product.objects.create(name='کالا', slug='prefill-test-product', erp_code='ERP-PREFILL-1',
                                         category=category, price=100000, stock=10)
        CartItem.objects.create(cart=Cart.objects.create(user=self.user), product=product, quantity=1)
        self.client.force_login(self.user)

    def test_without_address_uses_profile_names_and_phone(self):
        html = self.client.get(reverse('orders:checkout')).content.decode()
        self.assertIn('value="علی"', html)
        self.assertIn('value="09120000031"', html)

    def test_prefilled_from_default_address(self):
        from accounts.models import Address
        from locations.models import City, Province
        city = City.objects.create(province=Province.objects.create(name='استان پیش‌پر'), name='شهر پیش‌پر')
        Address.objects.create(user=self.user, title='منزل', receiver_first_name='مریم', receiver_last_name='کاظمی',
                               receiver_phone='09123334455', city=city, postal_code='1112223334', address='بلوار پیش‌پر')
        html = self.client.get(reverse('orders:checkout')).content.decode()
        for expected in ('value="مریم"', 'value="کاظمی"', 'value="09123334455"', 'value="1112223334"',
                         'استان پیش‌پر، شهر پیش‌پر، بلوار پیش‌پر'):
            with self.subTest(expected=expected):
                self.assertIn(expected, html)
