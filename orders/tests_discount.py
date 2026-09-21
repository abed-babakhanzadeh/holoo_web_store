"""
تست اسنپ‌شات تخفیف روی Order/OrderItem و رفتار زمانِ سرور در مسیر کامل سبد ← تسویه ← ثبت سفارش.

اصل‌ها:
  ۱. OrderItem.price = قیمت بعد از تخفیف خودکار؛ original_price/discount_amount قیمت اصلی و تخفیف «هر واحد»‌اند
     و Order.promotion_discount جمع آن‌ها؛ هیچ‌کدام بعد از ثبت با تغییر کمپین‌ها عوض نمی‌شود.
  ۲. مبلغ نمایش‌داده‌شده در تسویه = مبلغ ثبت‌شده در سفارش؛ ساعت مرجع فقط timezone.now() سرور است.
"""

from datetime import timedelta
from unittest import mock

from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomUser
from cart.models import Cart, CartItem
from orders.models import Order, OrderItem
from orders.tests import CheckoutTestBase
from products.models import Product
from promotions.models import DiscountPolicy, Promotion
from promotions.testing import make_promotion, reset_promotions_cache


class DiscountSnapshotTests(CheckoutTestBase):
    def _submit(self, **overrides):
        data = {'address_id': self.address.pk, 'payment_method': 'check'}
        data.update(overrides)
        data = {k: v for k, v in data.items() if v is not None}
        with mock.patch('holoo.receivers.send_order_to_holoo'):
            with self.captureOnCommitCallbacks(execute=True):
                return self.post_order(data)

    def _order(self):
        return Order.objects.get(user=self.user)

    def other_product(self, price=50000, price2=45000):
        n = Product.objects.count() + 1
        return Product.objects.create(name=f'کالای دوم {n}', slug=f'order-discount-p-{n}', erp_code=f'ERP-OD-{n}',
                                      category=self.product.category, price=price, price2=price2, stock=10)

    # ---------- ثبت اسنپ‌شات ----------
    def test_discounted_item_snapshots_original_price_and_unit_discount(self):
        make_promotion(self.product, percent=20)
        self._submit()
        order = self._order()
        item = order.items.get()
        self.assertEqual((item.original_price, item.discount_amount, item.price, item.quantity),
                         (100000, 20000, 80000, 2))
        self.assertEqual(order.promotion_discount, 40000)                    # ۲۰٬۰۰۰ × ۲ واحد
        self.assertEqual(order.order_discount, 0)
        self.assertEqual(order.order_discount_label, '')
        self.assertEqual(order.total_price, 160000)                          # ۲ × ۸۰٬۰۰۰ + ارسال پستی (۰)

    def test_undiscounted_order_still_snapshots_the_base_price(self):
        self._submit()
        order = self._order()
        item = order.items.get()
        self.assertEqual((item.original_price, item.discount_amount, item.price), (100000, 0, 100000))
        self.assertEqual(order.promotion_discount, 0)
        self.assertFalse(order.has_discount)
        self.assertEqual(item.unit_original_price, 100000)

    def test_original_price_follows_the_payment_method(self):
        make_promotion(self.product, percent=10)
        self._submit(payment_method='cash')
        item = self._order().items.get()
        self.assertEqual((item.original_price, item.discount_amount, item.price), (90000, 9000, 81000))

    def test_multi_item_order_totals_and_invariants(self):
        second = self.other_product()
        CartItem.objects.create(cart=self.cart, product=second, quantity=3)
        make_promotion(self.product, percent=20)
        make_promotion(second, kind='fixed', value=5000)
        self._submit()
        order = self._order()
        items = list(order.items.order_by('pk'))
        self.assertEqual(sum(i.discount_amount * i.quantity for i in items), order.promotion_discount)
        self.assertEqual(order.promotion_discount, 20000 * 2 + 5000 * 3)
        for item in items:
            self.assertEqual(item.original_price, item.price + item.discount_amount)
        self.assertEqual(order.items_original_total, 100000 * 2 + 50000 * 3)
        self.assertEqual(order.items_total, order.items_original_total - order.promotion_discount)
        self.assertEqual(order.total_price, order.computed_total)
        self.assertEqual(order.total_discount, order.promotion_discount)

    def test_courier_order_total_equals_items_plus_shipping_minus_order_discount(self):
        make_promotion(self.product, percent=10)
        courier = self.make_address(self.user, self.zoned_city, self.zone, title='پیکی')
        self._submit(address_id=courier.pk)
        order = self._order()
        self.assertEqual(order.total_price, 180000 + 45000)
        self.assertEqual(order.total_price, order.computed_total)

    def test_vip_user_snapshot_has_no_discount_by_default(self):
        self.user.price_level = 3
        self.user.save(update_fields=['price_level'])
        self.product.price3 = 70000
        self.product.save(update_fields=['price3'])
        make_promotion(self.product, percent=20)
        self._submit()
        order = self._order()
        item = order.items.get()
        self.assertEqual((item.original_price, item.discount_amount, item.price), (70000, 0, 70000))
        self.assertEqual(order.promotion_discount, 0)

    # ---------- ثابت‌ماندن بعد از تغییر کمپین ----------
    def test_later_campaign_changes_never_touch_an_existing_order(self):
        promotion = make_promotion(self.product, percent=20)
        self._submit()
        order = self._order()
        before = (order.total_price, order.promotion_discount, list(order.items.values_list('price', 'original_price', 'discount_amount')))

        promotion.value = 50                                                # ویرایش درصد
        promotion.save()
        Promotion.objects.filter(pk=promotion.pk).update(ends_at=timezone.now() - timedelta(days=3))   # پایان یافتن
        reset_promotions_cache()
        promotion.delete()                                                  # حذف کامل کمپین
        policy = DiscountPolicy.load()
        policy.promotions_enabled = False
        policy.save()
        self.product.price, self.product.price2 = 999999, 888888           # قیمت جدید از هلو
        self.product.save()

        order.refresh_from_db()
        after = (order.total_price, order.promotion_discount, list(order.items.values_list('price', 'original_price', 'discount_amount')))
        self.assertEqual(after, before)
        self.assertEqual(order.items_total, 160000)

    def test_deleting_the_product_keeps_the_snapshot(self):
        make_promotion(self.product, percent=20)
        self._submit()
        self.product.delete()
        item = self._order().items.get()
        self.assertIsNone(item.product)
        self.assertEqual((item.original_price, item.discount_amount, item.price), (100000, 20000, 80000))

    def test_order_has_no_foreign_key_to_promotions(self):
        for model in (Order, OrderItem):
            related = {f.related_model for f in model._meta.get_fields() if f.is_relation and f.related_model}
            self.assertFalse({m for m in related if m._meta.app_label == 'promotions'}, model.__name__)

    # ---------- قید‌های دیتابیس ----------
    def test_database_rejects_an_item_whose_snapshot_does_not_add_up(self):
        order = Order.objects.create(user=self.user, first_name='الف', last_name='ب', phone='09120000000', address='x',
                                     total_price=100)
        with self.assertRaises(IntegrityError), transaction.atomic():
            OrderItem.objects.create(order=order, product=self.product, price=80000, original_price=100000, discount_amount=1000)

    def test_database_rejects_negative_discounts(self):
        order = Order.objects.create(user=self.user, first_name='الف', last_name='ب', phone='09120000000', address='x',
                                     total_price=100)
        with self.assertRaises(IntegrityError), transaction.atomic():
            OrderItem.objects.create(order=order, product=self.product, price=100, original_price=0, discount_amount=-5)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Order.objects.create(user=self.user, first_name='الف', last_name='ب', phone='09120000000', address='x',
                                 total_price=100, promotion_discount=-1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Order.objects.create(user=self.user, first_name='الف', last_name='ب', phone='09120000000', address='x',
                                 total_price=100, order_discount=-1)

    def test_legacy_shaped_rows_without_a_snapshot_are_allowed(self):
        order = Order.objects.create(user=self.user, first_name='الف', last_name='ب', phone='09120000000', address='x',
                                     total_price=100)
        item = OrderItem.objects.create(order=order, product=self.product, price=100000, quantity=1)
        self.assertEqual((item.original_price, item.discount_amount), (0, 0))
        self.assertEqual((item.unit_original_price, item.original_cost, item.line_discount, item.has_discount), (100000, 100000, 0, False))

    def test_new_field_defaults_are_ascii_so_sql_server_ddl_cannot_corrupt_persian_letters(self):
        for model, names in ((Order, ('promotion_discount', 'order_discount', 'order_discount_label')),
                             (OrderItem, ('original_price', 'discount_amount'))):
            for name in names:
                default = model._meta.get_field(name).default
                self.assertTrue(str(default).isascii(), (model.__name__, name, default))

    def test_persian_discount_label_round_trips_with_correct_letters(self):
        order = Order.objects.create(user=self.user, first_name='الف', last_name='ب', phone='09120000000', address='x',
                                     total_price=100, order_discount=500, order_discount_label='کوپن یلدا')
        self.assertEqual(Order.objects.get(pk=order.pk).order_discount_label, 'کوپن یلدا')

    def test_discount_percent_property(self):
        make_promotion(self.product, percent=25)
        self._submit()
        self.assertEqual(self._order().items.get().discount_percent, 25)


class ServerTimeInCheckoutFlowTests(CheckoutTestBase):
    """ نمایش قیمت، سبد، فاکتور زنده و ثبت نهایی همه فقط با ساعت سرور؛ ساعت/فیلد/هدر کلاینت اثری ندارد """

    def setUp(self):
        super().setUp()
        self.promotion = make_promotion(self.product, percent=20)
        self.end = self.promotion.ends_at

    def at(self, now):
        """ ساعت سرور را روی «now» می‌گذارد (شاخص تخفیف پیش از هر بررسی دوباره از دیتابیس ساخته می‌شود) """
        reset_promotions_cache()
        return mock.patch('django.utils.timezone.now', return_value=now)

    def invoice(self, **params):
        params.setdefault('payment_method', 'check')
        params.setdefault('address_id', self.address.pk)
        return self.client.get(reverse('orders:update_invoice'), params)

    def submit(self, **extra):
        data = {'address_id': self.address.pk, 'payment_method': 'check'}
        data.update(extra)
        with mock.patch('holoo.receivers.send_order_to_holoo'):
            with self.captureOnCommitCallbacks(execute=True):
                return self.post_order(data)

    def test_price_cart_invoice_and_order_all_agree_while_the_promotion_is_active(self):
        before_end = self.end - timedelta(seconds=30)
        with self.at(before_end):
            self.assertEqual(self.cart.get_total_price(), 160000)
            response = self.invoice()
            self.assertEqual(response.context['final_total'], 160000)
            self.assertContains(response, 'تخفیف کالاها')
            self.submit()
        order = Order.objects.get(user=self.user)
        self.assertEqual(order.total_price, response.context['final_total'])           # آنچه دیده شد = آنچه ثبت شد

    def test_all_stages_lose_the_discount_after_expiry_by_server_time(self):
        after_end = self.end + timedelta(seconds=1)
        with self.at(after_end):
            self.assertEqual(self.cart.get_total_price(), 200000)
            response = self.invoice()
            self.assertEqual(response.context['final_total'], 200000)
            self.assertNotContains(response, 'تخفیف کالاها')
            self.submit()
        order = Order.objects.get(user=self.user)
        self.assertEqual((order.total_price, order.promotion_discount), (200000, 0))
        self.assertEqual(order.items.get().price, 100000)

    def test_the_exact_end_instant_is_still_discounted_and_one_microsecond_later_is_not(self):
        with self.at(self.end):
            self.assertEqual(self.invoice().context['final_total'], 160000)
        with self.at(self.end + timedelta(microseconds=1)):
            self.assertEqual(self.invoice().context['final_total'], 200000)

    def test_promotion_expiring_between_invoice_and_submit_charges_the_current_server_price(self):
        """ باکس فاکتور با تخفیف دیده شده ولی تا لحظه‌ی ثبت منقضی شده؛ ثبت با قیمت لحظه‌ی ثبت است و با خودش سازگار """
        with self.at(self.end - timedelta(seconds=1)):
            self.assertEqual(self.invoice().context['final_total'], 160000)
        with self.at(self.end + timedelta(seconds=1)):
            self.submit()
        order = Order.objects.get(user=self.user)
        self.assertEqual(order.total_price, order.computed_total)
        self.assertEqual((order.total_price, order.promotion_discount), (200000, 0))

    def test_posted_times_and_client_headers_cannot_change_the_price(self):
        after_end = self.end + timedelta(days=1)
        forged = {'now': (self.end - timedelta(hours=1)).isoformat(), 'client_time': '2000-01-01T00:00:00',
                  'timestamp': '0', 'date': 'Sat, 01 Jan 2000 00:00:00 GMT'}
        headers = {'HTTP_DATE': 'Sat, 01 Jan 2000 00:00:00 GMT', 'HTTP_X_CLIENT_TIME': '2000-01-01T00:00:00',
                   'HTTP_X_TIMEZONE_OFFSET': '-999999'}
        with self.at(after_end):
            self.assertEqual(self.client.get(reverse('orders:update_invoice'), {
                'payment_method': 'check', 'address_id': self.address.pk, **forged}, **headers).context['final_total'], 200000)
            with mock.patch('holoo.receivers.send_order_to_holoo'):
                with self.captureOnCommitCallbacks(execute=True):
                    self.client.post(reverse('orders:submit_order'),
                                     {'address_id': self.address.pk, 'payment_method': 'check',
                                      'expected_total': 200000, **forged}, **headers)
        self.assertEqual(Order.objects.get(user=self.user).total_price, 200000)

    def test_product_card_and_detail_use_server_time_too(self):
        detail_url = reverse('products:product_detail', args=[self.product.slug])
        with self.at(self.end - timedelta(seconds=5)):
            self.assertIn('٪20', self.client.get(detail_url).content.decode())
        with self.at(self.end + timedelta(seconds=5)):
            self.assertNotIn('٪20', self.client.get(detail_url).content.decode())

    def test_countdown_markup_carries_the_server_clock_not_a_client_one(self):
        response = self.client.get(reverse('products:home'))
        html = response.content.decode()
        self.assertIn('data-server-now="', html)
        self.assertIn('data-deal-ends="', html)
        script = html[html.index('countdown-timer.js'):][:80]
        self.assertIn('?v=', script)                                        # کش‌شکنی نسخه‌ی جدید تایمر

    def test_countdown_script_never_trusts_the_device_clock(self):
        import pathlib
        from django.conf import settings
        path = next(pathlib.Path(d) for d in settings.STATICFILES_DIRS
                    if (pathlib.Path(d) / 'theme/assets/js/dependencies/countdown-timer.js').exists())
        source = (path / 'theme/assets/js/dependencies/countdown-timer.js').read_text(encoding='utf-8')
        self.assertIn('data-server-now', source.replace('dataset.serverNow', 'data-server-now'))
        self.assertIn('performance.now()', source)
        code = '\n'.join(line for line in source.splitlines() if not line.strip().startswith(('/*', '*', '//')))
        # تنها جایی که Date.now مجاز است، جایگزینِ قالب‌های بدون data-server-now است
        self.assertEqual(code.count('Date.now()'), 1)


class OrderPagesShowDiscountTests(CheckoutTestBase):
    def place(self, **fields):
        with mock.patch('holoo.receivers.send_order_to_holoo'):
            with self.captureOnCommitCallbacks(execute=True):
                self.post_order({'address_id': self.address.pk, 'payment_method': 'check', **fields})
        return Order.objects.get(user=self.user)

    def test_full_detail_page_shows_original_discount_and_totals(self):
        make_promotion(self.product, percent=20)
        order = self.place()
        html = self.client.get(reverse('orders:order_detail_full', args=[order.pk])).content.decode()
        self.assertIn('جمع محصولات (قبل از تخفیف)', html)
        self.assertIn('تخفیف کالاها', html)
        self.assertIn('جمع محصولات (پس از تخفیف)', html)
        self.assertIn('٪20 تخفیف', html)
        self.assertIn('200000', html)                                       # قبل از تخفیف
        self.assertIn('40000', html)                                        # تخفیف
        self.assertIn('160000', html)                                       # قابل پرداخت

    def test_full_detail_page_of_an_undiscounted_order_is_unchanged(self):
        order = self.place()
        html = self.client.get(reverse('orders:order_detail_full', args=[order.pk])).content.decode()
        self.assertNotIn('تخفیف کالاها', html)
        self.assertNotIn('قبل از تخفیف', html)
        self.assertIn('جمع محصولات', html)

    def test_legacy_order_without_a_snapshot_renders_without_discount_lines(self):
        legacy = Order.objects.create(user=self.user, first_name='الف', last_name='ب', phone='09120000000', address='x',
                                      total_price=100000)
        OrderItem.objects.create(order=legacy, product=self.product, price=100000, quantity=1)
        response = self.client.get(reverse('orders:order_detail_full', args=[legacy.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'تخفیف کالاها')
        self.assertContains(response, '100000')

    def test_order_level_discount_line_uses_its_label(self):
        order = self.place()
        Order.objects.filter(pk=order.pk).update(order_discount=5000, order_discount_label='کد YALDA')
        html = self.client.get(reverse('orders:order_detail_full', args=[order.pk])).content.decode()
        self.assertIn('کد YALDA', html)
        self.assertIn('5000', html)

    def test_dashboard_detail_partial_shows_the_total_discount(self):
        make_promotion(self.product, percent=20)
        order = self.place()
        response = self.client.get(reverse('orders:order_detail', args=[order.pk]))
        self.assertContains(response, 'مجموع تخفیف این سفارش')
        self.assertContains(response, '40000')

    def test_checkout_page_and_invoice_show_original_price_percent_and_discount(self):
        make_promotion(self.product, percent=20)
        page = self.client.get(reverse('orders:checkout'))
        self.assertContains(page, '٪20 تخفیف')
        self.assertContains(page, '<del')
        invoice = self.client.get(reverse('orders:update_invoice'), {'payment_method': 'check', 'address_id': self.address.pk})
        self.assertContains(invoice, 'مبلغ کالاها (قبل از تخفیف)')
        self.assertContains(invoice, 'مبلغ کالاها (پس از تخفیف)')
        self.assertEqual(invoice.context['pricing'].promotion_discount, 40000)
        self.assertEqual(invoice.context['final_total'], 160000)

    def test_checkout_without_discount_shows_no_discount_lines(self):
        invoice = self.client.get(reverse('orders:update_invoice'), {'payment_method': 'check', 'address_id': self.address.pk})
        self.assertNotContains(invoice, 'تخفیف کالاها')
        self.assertContains(invoice, 'مبلغ کالاها:')

    def test_cart_update_htmx_rows_come_from_the_same_pricing(self):
        make_promotion(self.product, percent=20)
        response = self.client.post(reverse('orders:checkout_cart_update', args=[self.product.pk, 'add']),
                                    {'payment_method': 'check'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['pricing'].items_total, 3 * 80000)
        self.assertContains(response, '٪20 تخفیف')

    def test_payable_amount_is_identical_in_invoice_and_saved_order_for_all_methods(self):
        make_promotion(self.product, percent=17)
        for method in ('check', 'cash'):
            with self.subTest(method=method):
                Order.objects.all().delete()
                Cart.objects.filter(user=self.user).delete()
                cart = Cart.objects.create(user=self.user)
                CartItem.objects.create(cart=cart, product=self.product, quantity=3)
                shown = self.client.get(reverse('orders:update_invoice'), {'payment_method': method, 'address_id': self.address.pk}).context['final_total']
                order = self.place(payment_method=method)
                self.assertEqual(order.total_price, shown)


class OrderAdminDiscountTests(CheckoutTestBase):
    def test_admin_shows_discount_snapshot_as_read_only(self):
        make_promotion(self.product, percent=20)
        with mock.patch('holoo.receivers.send_order_to_holoo'):
            with self.captureOnCommitCallbacks(execute=True):
                self.post_order({'address_id': self.address.pk, 'payment_method': 'check'})
        order = Order.objects.get(user=self.user)
        admin = CustomUser.objects.create_superuser(phone_number='09120000999')
        self.client.force_login(admin)
        response = self.client.get(reverse('admin:orders_order_change', args=[order.pk]))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        for name in ('promotion_discount', 'order_discount', 'order_discount_label', 'total_price'):
            self.assertNotIn(f'name="{name}"', html, name)                  # فقط‌خواندنی: input ندارد
        for name in ('original_price', 'discount_amount'):
            self.assertNotIn(f'name="items-0-{name}"', html, name)
        self.assertIn('40000', html)
        self.assertIn('تخفیف (اسنپ‌شات لحظه‌ی ثبت؛ غیرقابل ویرایش)', html)
