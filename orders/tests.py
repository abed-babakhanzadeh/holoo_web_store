"""تست تسویه‌حساب و ثبت سفارش: آدرسِ مالک‌سنجی‌شده، ارسال از روی آدرس، اسنپ‌شات، قفل‌شدن قیمت و انتشار رویداد."""

from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from accounts.models import Address, CustomUser
from cart.models import Cart, CartItem
from locations.models import City, DeliveryZone, Province
from orders.forms import CheckoutForm
from orders.models import Order
from products.models import Category, Product, SiteSettings
from promotions.testing import PromotionTestMixin, make_promotion


class CheckoutFormTests(TestCase):
    def test_address_id_is_returned_as_a_stripped_raw_string(self):
        form = CheckoutForm({'address_id': ' 12 ', 'payment_method': 'cash'})
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data['address_id'], '12')

    def test_everything_is_optional_at_form_level(self):
        form = CheckoutForm({})
        self.assertTrue(form.is_valid())
        self.assertEqual((form.cleaned_data['address_id'], form.cleaned_data['payment_method']), ('', ''))

    def test_tampered_payment_method_is_discarded(self):
        form = CheckoutForm({'payment_method': 'FREE'})
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data['payment_method'], '')

    def test_receiver_and_address_fields_are_no_longer_accepted(self):
        """ گیرنده/آدرس فقط از روی آدرسِ دیتابیس می‌آید؛ فرم دیگر چنین فیلدهایی ندارد """
        self.assertEqual(set(CheckoutForm().fields), {'address_id', 'payment_method'})


class CheckoutTestBase(PromotionTestMixin, TestCase):
    """ کاربر با آدرس پیش‌فرض در شهرِ پستی (پس‌کرایه)، آدرسِ پیکی با تعرفه، و یک سبد دو‌عددی """

    def setUp(self):
        super().setUp()
        self.addCleanup(cache.delete, SiteSettings.CACHE_KEY)      # کش Redis با rollback تراکنش تست پاک نمی‌شود
        self.user = CustomUser.objects.create_user(phone_number='09120000021', price_level=1)
        self.other = CustomUser.objects.create_user(phone_number='09120000023')
        province = Province.objects.create(name='استان تسویه‌ی آزمون')
        self.post_city = City.objects.create(province=province, name='شهر پستیِ تسویه')
        self.zoned_city = City.objects.create(province=province, name='شهر پیکیِ تسویه')
        self.zone = DeliveryZone.objects.create(city=self.zoned_city, name='ناحیه‌ی تسویه', shipping_cost=45000)
        self.unpriced_zone = DeliveryZone.objects.create(city=self.zoned_city, name='ناحیه‌ی بدون تعرفه', shipping_cost=0)

        self.address = self.make_address(self.user, self.post_city, title='منزل', address='خیابان پستی، پلاک ۱')
        category = Category.objects.create(name='تست', slug='order-test-cat')
        self.product = Product.objects.create(
            name='کالا', slug='order-test-product', erp_code='ERP-ORDER-1',
            category=category, price=100000, price2=90000, stock=10,
        )
        self.cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(cart=self.cart, product=self.product, quantity=2)
        self.client.force_login(self.user)

    def make_address(self, user, city, zone=None, **overrides):
        data = dict(user=user, title='آدرس', receiver_first_name='مریم', receiver_last_name='کاظمی',
                    receiver_phone='09123334455', city=city, zone=zone, postal_code='1112223334', address='بلوار آزمون')
        data.update(overrides)
        return Address.objects.create(**data)

    def current_total(self, data):
        """ مبلغی که فاکتور زنده همین حالا به کاربر نشان می‌دهد (همان چیزی که مرورگر در expected_total می‌فرستد) """
        response = self.client.get(reverse('orders:update_invoice'), {
            'payment_method': data.get('payment_method', 'check'), 'address_id': data.get('address_id', ''),
        })
        return int(response.context['final_total'])

    def post_order(self, data):
        """ ثبت سفارش مثل مرورگر: expected_total از فاکتور زنده گرفته می‌شود مگر اینکه تست صریحاً چیز دیگری بفرستد """
        data = dict(data)
        if 'expected_total' not in data:
            data['expected_total'] = self.current_total(data)
        elif data['expected_total'] is None:
            del data['expected_total']
        return self.client.post(reverse('orders:submit_order'), data)

    def set_policy(self, **fields):
        settings_obj = SiteSettings.load()
        for name, value in fields.items():
            setattr(settings_obj, name, value)
        settings_obj.save()


class SubmitOrderTests(CheckoutTestBase):
    def _submit(self, **overrides):
        data = {'address_id': self.address.pk, 'payment_method': 'check'}
        data.update(overrides)
        data = {k: v for k, v in data.items() if v is not None}
        # رویداد order_placed داخل on_commit منتشر می‌شود و در TestCase (که کل تست را در
        # یک تراکنش rollback‌شونده می‌پیچد) به‌خودی‌خود اجرا نمی‌شود
        with mock.patch('holoo.receivers.send_order_to_holoo') as task:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.post_order(data)
        return response, task

    def _order(self):
        return Order.objects.get(user=self.user)

    # ---------- قیمت و فاکتور ----------
    def test_order_is_created_with_frozen_prices(self):
        self._submit()
        order = self._order()
        item = order.items.get()

        self.assertEqual(item.price, Decimal('100000'))
        self.assertEqual(item.quantity, 2)
        self.assertEqual(order.total_price, Decimal('200000') + order.shipping_cost)

    def test_invoice_total_equals_sum_of_rows(self):
        self._submit(address_id=self.make_address(self.user, self.zoned_city, self.zone).pk)
        order = self._order()
        rows = sum(i.price * i.quantity for i in order.items.all())
        self.assertEqual(order.total_price, rows + order.shipping_cost)

    def test_active_discount_is_charged(self):
        """ باگ اصلی: تخفیف روی کارت نمایش داده می‌شد ولی در فاکتور اعمال نمی‌شد """
        make_promotion(self.product, percent=20)
        self._submit()
        self.assertEqual(self._order().items.get().price, Decimal('80000'))

    def test_payment_method_changes_charged_price(self):
        self._submit(payment_method='cash')
        self.assertEqual(self._order().items.get().price, Decimal('90000'))

    def test_vip_user_cannot_downgrade_to_cheaper_method(self):
        self.user.price_level = 3
        self.user.save(update_fields=['price_level'])
        self.product.price3 = 70000
        self.product.save(update_fields=['price3'])

        self._submit(payment_method='check')
        order = self._order()
        self.assertEqual(order.payment_method, 'vip')
        self.assertEqual(order.items.get().price, Decimal('70000'))

    # ---------- ارسال از روی آدرس + اسنپ‌شات ----------
    def test_postage_city_order_has_zero_shipping_and_postage_snapshot(self):
        self._submit()
        order = self._order()
        self.assertEqual((order.shipping_method, int(order.shipping_cost)), ('post', 0))
        self.assertEqual(order.shipping_label, 'پس‌کرایه (پرداخت هزینه درب منزل)')
        self.assertEqual(order.total_price, Decimal('200000'))                 # مبلغی به فاکتور اضافه نشد
        self.assertEqual((order.province, order.city, order.zone), ('استان تسویه‌ی آزمون', 'شهر پستیِ تسویه', ''))

    def test_courier_order_charges_the_zone_tariff_and_snapshots_the_zone(self):
        courier = self.make_address(self.user, self.zoned_city, self.zone, title='پیکی')
        self._submit(address_id=courier.pk)
        order = self._order()
        self.assertEqual((order.shipping_method, int(order.shipping_cost)), ('courier', 45000))
        self.assertEqual(order.total_price, Decimal('200000') + 45000)
        self.assertEqual((order.city, order.zone), ('شهر پیکیِ تسویه', 'ناحیه‌ی تسویه'))
        self.assertEqual(order.full_address, 'استان تسویه‌ی آزمون، شهر پیکیِ تسویه، ناحیه‌ی تسویه، بلوار آزمون')

    def test_snapshot_copies_receiver_from_the_chosen_address_not_from_the_profile(self):
        self.user.first_name, self.user.last_name = 'پروفایل', 'کاربر'
        self.user.save()
        self._submit()
        order = self._order()
        self.assertEqual((order.first_name, order.last_name, order.phone, order.postal_code),
                         ('مریم', 'کاظمی', '09123334455', '1112223334'))
        self.assertEqual(order.address, 'خیابان پستی، پلاک ۱')                   # فقط بخش خیابان؛ بقیه در full_address

    def test_a_non_default_address_can_be_chosen(self):
        courier = self.make_address(self.user, self.zoned_city, self.zone, title='پیکی')
        self.assertFalse(courier.is_default)
        self._submit(address_id=courier.pk)
        self.assertEqual(self._order().shipping_method, 'courier')

    def test_posted_price_and_receiver_fields_are_ignored(self):
        """ مرورگر هیچ مبلغ/گیرنده‌ای نمی‌تواند تحمیل کند؛ فقط شناسه‌ی آدرس و روش پرداخت خوانده می‌شود """
        courier = self.make_address(self.user, self.zoned_city, self.zone, title='پیکی')
        self._submit(address_id=courier.pk, shipping_cost='1', total_price='1', first_name='جعلی', address='جای دیگر',
                     phone='09999999999', shipping_method='post', zone='ناحیه‌ی جعلی')
        order = self._order()
        self.assertEqual((order.shipping_method, int(order.shipping_cost), order.zone), ('courier', 45000, 'ناحیه‌ی تسویه'))
        self.assertEqual((order.first_name, order.phone, order.address), ('مریم', '09123334455', 'بلوار آزمون'))
        self.assertEqual(order.total_price, Decimal('245000'))

    def test_free_shipping_cart_courier_is_free_only_while_the_policy_is_on(self):
        self.product.free_shipping = True
        self.product.save(update_fields=['free_shipping'])
        courier = self.make_address(self.user, self.zoned_city, self.zone, title='پیکی')

        self._submit(address_id=courier.pk)
        self.assertEqual(int(self._order().shipping_cost), 0)

        Order.objects.all().delete()
        Cart.objects.filter(user=self.user).delete()
        cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(cart=cart, product=self.product, quantity=2)
        self.set_policy(courier_free_for_free_shipping_cart=False)
        self._submit(address_id=courier.pk)
        self.assertEqual(int(self._order().shipping_cost), 45000)

    def test_free_shipping_needs_every_item_to_qualify(self):
        other = Product.objects.create(
            name='کالای دوم', slug='order-test-product-2', erp_code='ERP-ORDER-2',
            category=self.product.category, price=50000, stock=10, free_shipping=False,
        )
        CartItem.objects.create(cart=self.cart, product=other, quantity=1)
        self.product.free_shipping = True
        self.product.save(update_fields=['free_shipping'])
        courier = self.make_address(self.user, self.zoned_city, self.zone, title='پیکی')

        self._submit(address_id=courier.pk)
        self.assertEqual(int(self._order().shipping_cost), 45000)

    # ---------- مالکیت آدرس ----------
    def test_another_users_address_is_rejected(self):
        foreign = self.make_address(self.other, self.post_city, title='مال دیگری')
        response, task = self._submit(address_id=foreign.pk)

        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'آدرس انتخاب‌شده معتبر نیست', status_code=400)
        self.assertFalse(Order.objects.exists())
        self.assertTrue(Cart.objects.filter(user=self.user).exists())
        self.assertEqual(task.delay.call_count, 0)
        self.assertNotContains(response, 'مال دیگری', status_code=400)          # اطلاعات آدرسِ دیگران نشت نمی‌کند

    def test_nonexistent_or_malformed_address_ids_are_rejected(self):
        for bad in ('999999', 'abc', '1 OR 1=1', '-1', '0'):
            with self.subTest(address_id=bad):
                response, _ = self._submit(address_id=bad)
                self.assertEqual(response.status_code, 400)
        self.assertFalse(Order.objects.exists())

    def test_missing_address_id_is_blocked_even_if_the_user_has_a_default(self):
        response, task = self._submit(address_id=None)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'یک آدرس تحویل انتخاب کنید')
        self.assertFalse(Order.objects.exists())
        self.assertEqual(task.delay.call_count, 0)

    # ---------- آدرس‌های مسدود ----------
    def _assert_blocked(self, response, task, message):
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, message)
        self.assertFalse(Order.objects.exists())
        self.assertTrue(Cart.objects.filter(user=self.user).exists())            # سبد دست‌نخورده می‌ماند
        self.assertEqual(task.delay.call_count, 0)

    def test_zone_without_tariff_blocks_the_order(self):
        blocked = self.make_address(self.user, self.zoned_city, self.unpriced_zone, title='بی‌تعرفه')
        response, task = self._submit(address_id=blocked.pk)
        self._assert_blocked(response, task, 'تعرفه ارسال به این ناحیه هنوز تعیین نشده است؛ لطفاً با پشتیبانی تماس بگیرید.')

    def test_unset_tariff_blocks_even_a_free_shipping_cart(self):
        self.product.free_shipping = True
        self.product.save(update_fields=['free_shipping'])
        blocked = self.make_address(self.user, self.zoned_city, self.unpriced_zone, title='بی‌تعرفه')
        response, task = self._submit(address_id=blocked.pk)
        self._assert_blocked(response, task, 'تعرفه ارسال به این ناحیه هنوز تعیین نشده است')

    def test_disabled_postage_blocks_orders_to_postage_cities(self):
        self.set_policy(postage_collect_enabled=False, postage_disabled_message='فعلاً فقط داخل قم ارسال داریم.')
        response, task = self._submit()
        self._assert_blocked(response, task, 'فعلاً فقط داخل قم ارسال داریم.')

    def test_zoned_city_address_without_zone_blocks_the_order(self):
        legacy = self.make_address(self.user, self.post_city, title='قدیمی')
        Address.objects.filter(pk=legacy.pk).update(city=self.zoned_city)         # مثل آدرسِ منتقل‌شده‌ی قدیمی
        response, task = self._submit(address_id=legacy.pk)
        self._assert_blocked(response, task, 'انتخاب ناحیه الزامی است')

    def test_blocked_rerender_keeps_the_chosen_address_selected(self):
        blocked = self.make_address(self.user, self.zoned_city, self.unpriced_zone, title='بی‌تعرفه')
        response, _ = self._submit(address_id=blocked.pk)
        self.assertContains(response, f'value="{blocked.pk}" checked')

    def test_no_address_at_all_cannot_place_an_order(self):
        Address.objects.filter(user=self.user).delete()
        response, task = self._submit(address_id=None)
        self._assert_blocked(response, task, 'یک آدرس تحویل انتخاب کنید')

    # ---------- بقیه‌ی رفتارهای ثبت سفارش ----------
    def test_cart_is_emptied_after_submit(self):
        self._submit()
        self.assertFalse(Cart.objects.filter(user=self.user).exists())

    def test_order_placed_signal_reaches_accounting(self):
        _, task = self._submit()
        task.delay.assert_called_once_with(self._order().id)

    def test_order_placed_notifies_customer(self):
        """ order_placed یک شنونده‌ی مستقل دیگر هم دارد: تایید سفارش برای مشتری """
        self.user.first_name = 'علی'
        self.user.save(update_fields=['first_name'])

        with mock.patch('notifications.receivers.notify') as notify_mock:
            self._submit()

        notify_mock.assert_called_once_with(self.user.phone_number, 'order_placed_customer', name='علی', order_id=self._order().id)


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


class CheckoutPageTests(CheckoutTestBase):
    """ صفحه‌ی تسویه‌حساب: کارت‌های آدرس، وضعیت قابل‌ارسال/مسدود هر کارت، کاربر بدون آدرس """

    def _get(self, **params):
        return self.client.get(reverse('orders:checkout'), params)

    def test_lists_only_own_addresses_and_preselects_the_default(self):
        self.make_address(self.other, self.post_city, title='کارت-متعلق-به-دیگری')
        second = self.make_address(self.user, self.zoned_city, self.zone, title='محل کار')
        response = self._get()
        self.assertContains(response, 'خیابان پستی، پلاک ۱')
        self.assertContains(response, 'محل کار')
        self.assertNotContains(response, 'کارت-متعلق-به-دیگری')
        self.assertContains(response, f'value="{self.address.pk}" checked')
        self.assertNotContains(response, f'value="{second.pk}" checked')

    def test_old_free_text_receiver_inputs_are_gone(self):
        html = self._get().content.decode()
        for name in ('name="first_name"', 'name="last_name"', 'name="phone"', 'name="postal_code"', 'name="address"'):
            with self.subTest(name=name):
                self.assertNotIn(name, html)
        self.assertIn('name="address_id"', html)

    def test_invoice_refreshes_when_the_address_changes(self):
        html = self._get().content.decode()
        self.assertIn("change from:[name='address_id']", html)
        self.assertIn("[name='address_id']:checked", html)

    def test_address_query_param_preselects_an_owned_address_only(self):
        second = self.make_address(self.user, self.zoned_city, self.zone, title='محل کار')
        self.assertContains(self._get(address=second.pk), f'value="{second.pk}" checked')
        foreign = self.make_address(self.other, self.post_city)
        response = self._get(address=foreign.pk)
        self.assertContains(response, f'value="{self.address.pk}" checked')        # به پیش‌فرضِ خودِ کاربر برمی‌گردد
        self.assertNotContains(response, f'value="{foreign.pk}"')

    def test_blocked_cards_show_the_reason_and_edit_link_when_a_zone_is_missing(self):
        unpriced = self.make_address(self.user, self.zoned_city, self.unpriced_zone, title='بی‌تعرفه')
        legacy = self.make_address(self.user, self.post_city, title='قدیمی')
        Address.objects.filter(pk=legacy.pk).update(city=self.zoned_city)
        html = self._get().content.decode()
        self.assertIn('تعرفه ارسال به این ناحیه هنوز تعیین نشده است', html)
        self.assertIn('انتخاب ناحیه الزامی است', html)
        self.assertIn(reverse('accounts:address_edit', args=[legacy.pk]), html)                 # فقط برای ناحیه‌ی ناقص
        self.assertNotIn(reverse('accounts:address_edit', args=[unpriced.pk]) + '?next', html)    # تعرفه را ادمین می‌گذارد

    def test_user_without_addresses_is_asked_to_add_one(self):
        Address.objects.filter(user=self.user).delete()
        response = self._get()
        self.assertContains(response, 'ابتدا یک آدرس تحویل ثبت کنید')
        self.assertContains(response, reverse('accounts:address_create'))
        self.assertNotContains(response, 'name="address_id"')

    def test_add_address_link_returns_to_checkout(self):
        html = self._get().content.decode()
        self.assertIn(reverse('accounts:address_create') + '?next=' + reverse('orders:checkout'), html)


class UpdateInvoiceTests(CheckoutTestBase):
    """ پاسخ htmx فاکتور: کرایه/برچسب ارسال، و غیرفعال شدن دکمه‌ی ثبت وقتی آدرس مسدود یا انتخاب‌نشده است """

    def _invoice(self, **params):
        params.setdefault('payment_method', 'check')
        return self.client.get(reverse('orders:update_invoice'), params)

    def _button_disabled(self, response):
        html = response.content.decode()
        tag = html[html.index('id="submit-order-btn"'):]
        tag = tag[:tag.index('>')]
        return 'disabled' in tag

    def test_postage_address_shows_the_label_and_enables_submit(self):
        response = self._invoice(address_id=self.address.pk)
        self.assertContains(response, 'پس‌کرایه (پرداخت هزینه درب منزل)')
        self.assertContains(response, '200000')
        self.assertFalse(self._button_disabled(response))

    def test_courier_address_shows_tariff_and_total(self):
        courier = self.make_address(self.user, self.zoned_city, self.zone)
        response = self._invoice(address_id=courier.pk)
        self.assertContains(response, 'ارسال با پیک')
        self.assertContains(response, '45000')
        self.assertContains(response, '245000')
        self.assertFalse(self._button_disabled(response))

    def test_free_shipping_cart_shows_free_courier(self):
        self.product.free_shipping = True
        self.product.save(update_fields=['free_shipping'])
        courier = self.make_address(self.user, self.zoned_city, self.zone)
        response = self._invoice(address_id=courier.pk)
        self.assertContains(response, 'ارسال رایگان با پیک')
        self.assertContains(response, 'رایگان')

    def test_unset_tariff_disables_submit_and_explains(self):
        blocked = self.make_address(self.user, self.zoned_city, self.unpriced_zone)
        response = self._invoice(address_id=blocked.pk)
        self.assertContains(response, 'تعرفه ارسال به این ناحیه هنوز تعیین نشده است؛ لطفاً با پشتیبانی تماس بگیرید.')
        self.assertContains(response, 'امکان ثبت سفارش با این آدرس وجود ندارد')
        self.assertTrue(self._button_disabled(response))
        self.assertContains(response, 'aria-disabled="true"')
        self.assertNotContains(response, '245000')

    def test_disabled_postage_disables_submit_with_the_configured_message(self):
        self.set_policy(postage_collect_enabled=False, postage_disabled_message='فعلاً فقط داخل قم ارسال داریم.')
        response = self._invoice(address_id=self.address.pk)
        self.assertContains(response, 'فعلاً فقط داخل قم ارسال داریم.')
        self.assertTrue(self._button_disabled(response))

    def test_no_address_selected_disables_submit(self):
        response = self._invoice()
        self.assertContains(response, 'یک آدرس تحویل انتخاب کنید')
        self.assertTrue(self._button_disabled(response))

    def test_another_users_address_is_treated_as_no_address_without_leaking_it(self):
        foreign = self.make_address(self.other, self.zoned_city, self.zone, title='مال دیگری', address='آدرس-خصوصی-دیگری')
        response = self._invoice(address_id=foreign.pk)
        self.assertTrue(self._button_disabled(response))
        self.assertNotContains(response, 'آدرس-خصوصی-دیگری')
        self.assertNotContains(response, '45000')

    def test_zoned_city_address_without_zone_disables_submit(self):
        legacy = self.make_address(self.user, self.post_city, title='قدیمی')
        Address.objects.filter(pk=legacy.pk).update(city=self.zoned_city)
        response = self._invoice(address_id=legacy.pk)
        self.assertContains(response, 'انتخاب ناحیه الزامی است')
        self.assertTrue(self._button_disabled(response))
