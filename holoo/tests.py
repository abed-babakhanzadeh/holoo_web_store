"""تست یکپارچه‌سازی حسابداری — idempotency، قفل، و بازبینی سفارش‌های جامانده."""

from unittest import mock

from django.core.cache import cache
from django.test import TestCase

from accounts.models import CustomUser
from holoo.locks import task_lock
from holoo.tasks import confirm_payment_in_holoo, reconcile_holoo_orders, send_order_to_holoo, sync_products_from_holoo
from orders.models import Order, OrderItem
from payments.models import Transaction
from products.models import Category, Product


class TaskLockTests(TestCase):
    def setUp(self):
        cache.delete('lock:test-lock')

    def test_second_holder_is_refused_then_released(self):
        with task_lock('test-lock', timeout=30) as first:
            self.assertTrue(first)
            with task_lock('test-lock', timeout=30) as second:
                self.assertFalse(second)
        with task_lock('test-lock', timeout=30) as third:
            self.assertTrue(third)

    def test_lock_is_released_even_if_body_raises(self):
        with self.assertRaises(RuntimeError):
            with task_lock('test-lock', timeout=30):
                raise RuntimeError('boom')
        with task_lock('test-lock', timeout=30) as again:
            self.assertTrue(again)


class OrderSyncTests(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(phone_number='09120000040', erp_code='CUST-1')
        category = Category.objects.create(name='تست', slug='holoo-test-cat')
        self.product = Product.objects.create(
            name='کالا', slug='holoo-test-product', erp_code='ERP-H-1',
            category=category, price=100000, stock=5,
        )
        self.order = Order.objects.create(
            user=self.user, first_name='علی', last_name='رضایی', phone='09120000040',
            address='تهران', payment_method='cash', shipping_cost=200000, total_price=300000,
        )
        OrderItem.objects.create(order=self.order, product=self.product, price=100000, quantity=1)
        for key in ('lock:holoo:invoice:%s' % self.order.id, 'lock:holoo:receipt:%s' % self.order.id):
            cache.delete(key)

    def _pay(self):
        return Transaction.objects.create(
            user=self.user, order=self.order, amount=self.order.total_price,
            authority='AUTH-%s' % self.order.id, ref_id='REF1', status='success',
        )

    # --- ثبت فاکتور ---

    def test_invoice_is_registered_and_status_advances(self):
        send_order_to_holoo(self.order.id)
        self.order.refresh_from_db()

        self.assertTrue(self.order.holoo_invoice_id)
        self.assertEqual(self.order.status, 'registered')

    def test_already_registered_order_is_not_sent_again(self):
        """ بدون این محافظ، هر تلاش مجدد یک فاکتور تازه در حسابداری می‌ساخت """
        send_order_to_holoo(self.order.id)
        self.order.refresh_from_db()
        first_invoice = self.order.holoo_invoice_id

        with mock.patch('holoo.client.HolooClient.insert_invoice') as insert:
            result = send_order_to_holoo(self.order.id)

        self.assertEqual(insert.call_count, 0)
        self.assertIn('Already registered', result)
        self.order.refresh_from_db()
        self.assertEqual(self.order.holoo_invoice_id, first_invoice)

    def test_order_without_sendable_items_is_not_sent(self):
        self.order.items.all().delete()
        with mock.patch('holoo.client.HolooClient.insert_invoice') as insert:
            send_order_to_holoo(self.order.id)
        self.assertEqual(insert.call_count, 0)

    def test_missing_order_does_not_retry_forever(self):
        self.assertEqual(send_order_to_holoo(999999), 'Order not found.')

    def test_shipping_line_uses_site_settings_erp_code(self):
        """ کد کالای هزینه ارسال دیگر هاردکد نیست؛ از SiteSettings.shipping_erp_code خوانده می‌شود """
        from products.models import SiteSettings
        self.addCleanup(cache.delete, SiteSettings.CACHE_KEY)  # کش Redis با rollback تراکنش تست پاک نمی‌شود
        settings_obj = SiteSettings.load()
        settings_obj.shipping_erp_code = 'SHIP-CUSTOM'
        settings_obj.save()

        with mock.patch('holoo.client.HolooClient.insert_invoice') as insert:
            insert.return_value = {'success': True, 'InvoiceCode': 'INV-SHIP'}
            send_order_to_holoo(self.order.id)

        payload = insert.call_args[0][0]
        shipping_rows = [row for row in payload['Items'] if row['ErpCode'] == 'SHIP-CUSTOM']
        self.assertEqual(len(shipping_rows), 1)
        self.assertEqual(shipping_rows[0]['Price'], float(self.order.shipping_cost))

    # --- سند دریافت وجه ---

    def test_receipt_requires_a_successful_payment(self):
        self.order.holoo_invoice_id = 'INV-1'
        self.order.save(update_fields=['holoo_invoice_id'])
        self.assertEqual(confirm_payment_in_holoo(self.order.id), 'Not paid.')

    def test_receipt_is_registered_and_status_advances(self):
        self._pay()
        self.order.holoo_invoice_id = 'INV_123'
        self.order.save(update_fields=['holoo_invoice_id'])

        confirm_payment_in_holoo(self.order.id)
        self.order.refresh_from_db()

        self.assertTrue(self.order.holoo_receipt_id)
        self.assertEqual(self.order.status, 'processing')

    def test_receipt_is_not_registered_twice(self):
        self._pay()
        self.order.holoo_invoice_id = 'INV_123'
        self.order.save(update_fields=['holoo_invoice_id'])
        confirm_payment_in_holoo(self.order.id)

        with mock.patch('holoo.client.HolooClient.register_payment') as register:
            result = confirm_payment_in_holoo(self.order.id)

        self.assertEqual(register.call_count, 0)
        self.assertIn('Already registered', result)

    def test_payment_before_invoice_retries_instead_of_giving_up(self):
        """
        باگ اصلی نسخه‌ی قبل: اگر کاربر زودتر از ثبت فاکتور پرداخت می‌کرد، تسک با
        "No Invoice" برمی‌گشت و سند دریافت وجه آن سفارش برای همیشه گم می‌شد.
        """
        self._pay()
        self.assertIsNone(self.order.holoo_invoice_id)

        with mock.patch('holoo.tasks.confirm_payment_in_holoo.retry', side_effect=RuntimeError('retried')):
            with self.assertRaises(RuntimeError):
                confirm_payment_in_holoo(self.order.id)

    # --- تور ایمنی ---

    def test_reconcile_requeues_orders_missing_invoice(self):
        Order.objects.filter(pk=self.order.pk).update(
            created_at=self.order.created_at.replace(year=self.order.created_at.year - 1)
        )
        with mock.patch('holoo.tasks.send_order_to_holoo.delay') as task:
            reconcile_holoo_orders()
        self.assertIn(self.order.id, [c.args[0] for c in task.call_args_list])

    def test_reconcile_skips_recent_orders(self):
        """ سفارش تازه‌ثبت‌شده هنوز در چرخه‌ی عادی است و نباید دوباره به صف برود """
        with mock.patch('holoo.tasks.send_order_to_holoo.delay') as task:
            reconcile_holoo_orders()
        self.assertNotIn(self.order.id, [c.args[0] for c in task.call_args_list])

    def test_reconcile_skips_canceled_orders(self):
        Order.objects.filter(pk=self.order.pk).update(
            status='canceled',
            created_at=self.order.created_at.replace(year=self.order.created_at.year - 1),
        )
        with mock.patch('holoo.tasks.send_order_to_holoo.delay') as task:
            reconcile_holoo_orders()
        self.assertNotIn(self.order.id, [c.args[0] for c in task.call_args_list])


class UserSyncAddressTests(TestCase):
    """ آدرس مشتری در هلو = آدرس پیش‌فرض کاربر؛ تغییرش مشتری را دوباره همگام می‌کند """

    def setUp(self):
        from accounts.models import Address
        from locations.models import City, Province
        self.user = CustomUser.objects.create_user(
            phone_number='09120000710', first_name='علی', last_name='رضایی', national_code='0012345678')
        self.city = City.objects.create(province=Province.objects.create(name='استان تست هلو'), name='شهر تست هلو')
        self.Address = Address

    def _address(self, **kw):
        data = dict(user=self.user, title='منزل', receiver_first_name='علی', receiver_last_name='رضایی',
                    receiver_phone='09121112233', city=self.city, postal_code='1234567890', address='خیابان تست')
        data.update(kw)
        return self.Address.objects.create(**data)

    def test_new_customer_is_sent_with_default_address_full_text(self):
        from holoo.tasks import sync_user_to_holoo
        self._address()
        with mock.patch('holoo.tasks.HolooClient') as client:
            client.return_value.insert_person.return_value = {'success': True, 'erp_code': 'E1'}
            sync_user_to_holoo.run(self.user.id)
        self.assertEqual(client.return_value.insert_person.call_args.kwargs['address'],
                         'استان تست هلو، شهر تست هلو، خیابان تست')

    def test_customer_without_address_is_sent_with_empty_address(self):
        from holoo.tasks import sync_user_to_holoo
        with mock.patch('holoo.tasks.HolooClient') as client:
            client.return_value.insert_person.return_value = {'success': True, 'erp_code': 'E2'}
            sync_user_to_holoo.run(self.user.id)
        self.assertEqual(client.return_value.insert_person.call_args.kwargs['address'], '')

    def test_existing_customer_update_uses_the_default_not_another_address(self):
        from holoo.tasks import sync_user_to_holoo
        self._address(title='قدیمی', address='آدرس پیش‌فرض')
        self._address(title='دیگر', address='آدرس دیگر')
        self.user.erp_code = 'ERP-EXISTING'
        self.user.save(update_fields=['erp_code'])
        with mock.patch('holoo.tasks.HolooClient') as client:
            client.return_value.update_person.return_value = {'success': True}
            sync_user_to_holoo.run(self.user.id)
        self.assertIn('آدرس پیش‌فرض', client.return_value.update_person.call_args.kwargs['address'])

    def test_default_address_change_triggers_sync_for_complete_profiles(self):
        with mock.patch('holoo.receivers.sync_user_to_holoo') as task, self.captureOnCommitCallbacks(execute=True):
            self._address()
        task.delay.assert_called_once_with(self.user.id)

    def test_default_address_change_is_ignored_while_profile_is_incomplete(self):
        incomplete = CustomUser.objects.create_user(phone_number='09120000711')
        with mock.patch('holoo.receivers.sync_user_to_holoo') as task, self.captureOnCommitCallbacks(execute=True):
            self._address(user=incomplete)
        task.delay.assert_not_called()


class ProductSyncBackInStockTests(TestCase):
    """ سینک محصولات هلو باید تشخیص دهد موجودی صفر به مثبت رسیده و رویداد دامنه اعلام کند """

    def setUp(self):
        category = Category.objects.create(name='تست', slug='sync-stock-test-cat')
        self.product = Product.objects.create(
            name='کالای تست موجودی', slug='sync-stock-test-product', erp_code='ERP-SYNC-STOCK-1',
            category=category, price=100000, stock=0,
        )
        cache.delete('lock:holoo:product_sync')

    def _fake_item(self, few):
        return {
            'ErpCode': 'ERP-SYNC-STOCK-1', 'Name': self.product.name, 'Code': 'CODE-1', 'Few': few,
            'SellPrice': 100000, 'SellPrice2': 0, 'SellPrice3': 0, 'SellPrice4': 0, 'SellPrice5': 0,
            'SellPrice6': 0, 'SellPrice7': 0, 'SellPrice8': 0, 'SellPrice9': 0, 'SellPrice10': 0,
            'IsActive': True,
        }

    def test_signal_fires_when_stock_goes_from_zero_to_positive(self):
        with mock.patch('holoo.client.HolooClient.get_product_count', return_value=1), \
             mock.patch('holoo.client.HolooClient.get_products', return_value={'product': [self._fake_item(5)]}), \
             mock.patch('products.signals.product_back_in_stock.send_robust') as signal_mock:
            sync_products_from_holoo()

        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 5)
        signal_mock.assert_called_once()
        self.assertEqual(signal_mock.call_args.kwargs['product'].id, self.product.id)

    def test_signal_does_not_fire_when_stock_stays_zero(self):
        with mock.patch('holoo.client.HolooClient.get_product_count', return_value=1), \
             mock.patch('holoo.client.HolooClient.get_products', return_value={'product': [self._fake_item(0)]}), \
             mock.patch('products.signals.product_back_in_stock.send_robust') as signal_mock:
            sync_products_from_holoo()

        self.assertEqual(signal_mock.call_count, 0)

    def test_signal_does_not_fire_when_already_in_stock(self):
        self.product.stock = 3
        self.product.save(update_fields=['stock'])
        with mock.patch('holoo.client.HolooClient.get_product_count', return_value=1), \
             mock.patch('holoo.client.HolooClient.get_products', return_value={'product': [self._fake_item(7)]}), \
             mock.patch('products.signals.product_back_in_stock.send_robust') as signal_mock:
            sync_products_from_holoo()

        self.assertEqual(signal_mock.call_count, 0)
