"""
نمایش سفارش: ادمین (اسنپ‌شات مقصد/ارسال فقط‌خواندنی) و قالب‌های سفارش کاربر (روش ارسال + آدرس کامل).
"""

from django.contrib import admin as django_admin
from django.test import RequestFactory, TestCase
from django.urls import reverse

from accounts.models import CustomUser
from orders.admin import OrderAdmin
from orders.models import Order


class OrderDisplayBase(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(phone_number='09120009001', first_name='مریم', last_name='کاظمی')
        self.admin_user = CustomUser.objects.create_superuser(phone_number='09120009002')

    def order(self, **overrides):
        data = dict(user=self.user, first_name='مریم', last_name='کاظمی', phone='09123334455', payment_method='cash',
                    address='بلوار پردیسان، فاز ۲', postal_code='3749113666', total_price=155000,
                    province='قم', city='قم', zone='پردیسان', shipping_method='courier',
                    shipping_label='ارسال با پیک', shipping_cost=45000)
        data.update(overrides)
        return Order.objects.create(**data)

    def post_order(self, **overrides):
        data = dict(province='تهران', city='تهران', zone='', address='خیابان ولیعصر، پلاک ۴', shipping_method='post',
                    shipping_label='پس‌کرایه (پرداخت هزینه درب منزل)', shipping_cost=0, total_price=110000)
        data.update(overrides)
        return self.order(**data)

    def legacy_order(self, **overrides):
        data = dict(province='', city='', zone='', address='تهران، خیابان آزادی، پلاک ۱', shipping_method='',
                    shipping_label='', shipping_cost=200000, total_price=310000)
        data.update(overrides)
        return self.order(**data)


class OrderModelDisplayTests(OrderDisplayBase):
    def test_shipping_title_per_method(self):
        self.assertEqual(self.order().shipping_title, 'ارسال با پیک')
        self.assertEqual(self.post_order().shipping_title, 'ارسال با پست (پس‌کرایه)')
        self.assertEqual(self.legacy_order().shipping_title, 'هزینه ارسال')


class OrderAdminTests(OrderDisplayBase):
    SNAPSHOT = ('province', 'city', 'zone', 'shipping_method', 'shipping_label')

    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin_user)

    def test_snapshot_fields_are_readonly(self):
        model_admin = OrderAdmin(Order, django_admin.site)
        request = RequestFactory().get('/')
        request.user = self.admin_user
        readonly = set(model_admin.get_readonly_fields(request, self.order()))
        self.assertTrue(set(self.SNAPSHOT) | {'full_address_display'} <= readonly)
        editable = set(model_admin.get_form(request, self.order()).base_fields)
        self.assertFalse(editable & (set(self.SNAPSHOT) | {'full_address_display'}))     # اصلاً در فرم ویرایش نیستند
        self.assertTrue({'status', 'tracking_code', 'first_name', 'address'} <= editable)  # فیلدهای عملیاتی ویرایش‌پذیرند

    def test_change_page_shows_the_snapshot_values(self):
        order = self.order()
        response = self.client.get(reverse('admin:orders_order_change', args=[order.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'مقصد و روش ارسال')
        self.assertContains(response, 'قم، قم، پردیسان، بلوار پردیسان، فاز ۲')          # آدرس کامل
        self.assertContains(response, 'ارسال با پیک')                                    # روش ارسال (نمایش انتخاب)
        for name in self.SNAPSHOT:
            with self.subTest(name=name):
                self.assertNotContains(response, f'name="{name}"')                        # ورودی قابل‌ویرایش نیست

    def test_post_tampering_cannot_change_the_snapshot(self):
        order = self.order()
        data = {
            'user': self.user.pk, 'status': 'processing', 'tracking_code': '', 'payment_method': 'cash', 'total_price': '155000',
            'shipping_cost': '45000', 'first_name': 'مریم', 'last_name': 'کاظمی', 'phone': '09123334455',
            'postal_code': '3749113666', 'address': 'بلوار پردیسان، فاز ۲',
            'holoo_invoice_id': '', 'holoo_receipt_id': '',
            # تلاش برای دستکاری اسنپ‌شات
            'province': 'جعلی', 'city': 'جعلی', 'zone': 'جعلی', 'shipping_method': 'post', 'shipping_label': 'جعلی',
            'items-TOTAL_FORMS': '0', 'items-INITIAL_FORMS': '0', 'items-MIN_NUM_FORMS': '0', 'items-MAX_NUM_FORMS': '1000',
        }
        response = self.client.post(reverse('admin:orders_order_change', args=[order.pk]), data)
        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.status, 'processing')                                   # تغییر عملیاتی اعمال شد
        self.assertEqual((order.province, order.city, order.zone, order.shipping_method, order.shipping_label),
                         ('قم', 'قم', 'پردیسان', 'courier', 'ارسال با پیک'))          # اسنپ‌شات دست‌نخورده

    def test_legacy_order_change_page_renders(self):
        legacy = self.legacy_order()
        response = self.client.get(reverse('admin:orders_order_change', args=[legacy.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'تهران، خیابان آزادی، پلاک ۱')

    def test_list_shows_city_and_method_and_filters_by_method(self):
        courier, post = self.order(), self.post_order()
        response = self.client.get(reverse('admin:orders_order_changelist'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'تهران')
        response = self.client.get(reverse('admin:orders_order_changelist'), {'shipping_method__exact': 'post'})
        self.assertContains(response, f'/{post.pk}/change/')
        self.assertNotContains(response, f'/{courier.pk}/change/')

    def test_search_by_city(self):
        courier, post = self.order(), self.post_order()
        response = self.client.get(reverse('admin:orders_order_changelist'), {'q': 'تهران'})
        self.assertContains(response, f'/{post.pk}/change/')
        self.assertNotContains(response, f'/{courier.pk}/change/')


class UserOrderTemplatesTests(OrderDisplayBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def full(self, order):
        return self.client.get(reverse('orders:order_detail_full', args=[order.pk])).content.decode()

    def card(self, order):
        return self.client.get(reverse('orders:order_detail', args=[order.pk])).content.decode()

    def success(self, order):
        return self.client.get(reverse('orders:order_success', args=[order.pk])).content.decode()

    # ---------- پیک ----------
    def test_courier_order_shows_method_full_address_and_amount(self):
        order = self.order()
        for name, html in (('full', self.full(order)), ('card', self.card(order)), ('success', self.success(order))):
            with self.subTest(page=name):
                self.assertIn('قم، قم، پردیسان، بلوار پردیسان، فاز ۲', html)
        html = self.full(order)
        self.assertIn('ارسال با پیک', html)
        self.assertIn('45000 تومان', html)
        self.assertNotIn('هنگام تحویل مرسوله توسط گیرنده', html)

    def test_free_courier_shows_free_instead_of_zero(self):
        html = self.full(self.order(shipping_cost=0, shipping_label='ارسال رایگان با پیک', total_price=110000))
        self.assertRegex(html, r'ارسال با پیک\s*</span>\s*<span[^>]*>\s*رایگان')

    # ---------- پست / پس‌کرایه ----------
    def test_postage_collect_order_shows_the_label_note_and_no_amount(self):
        order = self.post_order()
        html = self.full(order)
        self.assertIn('ارسال با پست (پس‌کرایه)', html)
        self.assertIn('پس‌کرایه (پرداخت هزینه درب منزل)', html)
        self.assertIn('هنگام تحویل مرسوله توسط گیرنده به پست پرداخت می‌شود', html)
        self.assertIn('تهران، تهران، خیابان ولیعصر، پلاک ۴', html)
        self.assertNotRegex(html, r'ارسال با پست \(پس‌کرایه\)\s*</span>\s*<span[^>]*>\s*0 تومان')      # «۰ تومان» نشان داده نمی‌شود
        card = self.card(order)
        self.assertIn('ارسال با پست (پس‌کرایه): <b', card)
        self.assertIn('پس‌کرایه (پرداخت هزینه درب منزل)', card)

    def test_success_page_mentions_method_and_address(self):
        html = self.success(self.post_order())
        self.assertIn('آدرس تحویل', html)
        self.assertIn('ارسال با پست (پس‌کرایه)', html)
        self.assertIn('پس‌کرایه (پرداخت هزینه درب منزل)', html)

    # ---------- سفارش قدیمی ----------
    def test_legacy_order_renders_exactly_like_before(self):
        order = self.legacy_order()
        html = self.full(order)
        self.assertIn('تهران، خیابان آزادی، پلاک ۱', html)
        self.assertRegex(html, r'هزینه ارسال\s*</span>\s*<span[^>]*>\s*200000 تومان')
        self.assertNotIn('روش ارسال:', html)                                             # سفارش قدیمی روش ارسال ندارد
        self.assertIn('هزینه ارسال: <b', self.card(order))
        self.assertIn('200000 تومان', self.card(order))

    def test_other_users_orders_are_not_visible(self):
        foreign = self.order(user=self.admin_user)
        self.assertEqual(self.client.get(reverse('orders:order_detail_full', args=[foreign.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('orders:order_detail', args=[foreign.pk])).status_code, 404)
