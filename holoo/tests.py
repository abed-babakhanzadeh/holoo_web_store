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


class InvoicePayloadTests(TestCase):
    """
    بدنه‌ی فاکتور هلو: آدرس کامل در «توضیحات» و ردیف کرایه فقط برای پیک (holoo/invoice.py)؛
    پس‌کرایه‌ی پست هرگز ردیف کرایه نمی‌سازد و سفارش‌های قدیمی (بدون روش ارسال) همان قاعده‌ی قبلی را دارند.
    """

    ITEMS = [{'ErpCode': 'ERP-P-1', 'Amount': 1, 'Price': 100000.0, 'Comment': 'ثبت از سایت - روش cash'}]

    def setUp(self):
        self.user = CustomUser.objects.create_user(phone_number='09120000730', erp_code='CUST-730')

    def order(self, **overrides):
        data = dict(user=self.user, first_name='مریم', last_name='کاظمی', phone='09123334455', payment_method='cash',
                    address='بلوار پردیسان، فاز ۲', postal_code='3749113666', total_price=100000,
                    province='قم', city='قم', zone='پردیسان', shipping_method='courier',
                    shipping_label='ارسال با پیک', shipping_cost=45000)
        data.update(overrides)
        return Order.objects.create(**data)

    def payload(self, order, code='SHIP-1'):
        from holoo.invoice import build_invoice_payload
        return build_invoice_payload(order, self.ITEMS, code)

    def shipping_rows(self, payload, code='SHIP-1'):
        return [row for row in payload['Items'] if row['ErpCode'] == code]

    # ---------- ردیف کرایه ----------
    def test_courier_order_gets_one_shipping_row_with_the_configured_erp_code(self):
        payload = self.payload(self.order(), code='SHIP-CUSTOM')
        rows = self.shipping_rows(payload, 'SHIP-CUSTOM')
        self.assertEqual(rows, [{'ErpCode': 'SHIP-CUSTOM', 'Amount': 1, 'Price': 45000.0, 'Comment': 'کرایه پیک (قم - پردیسان)'}])
        self.assertEqual(payload['Items'][:1], self.ITEMS)                          # ردیف کالا دست‌نخورده، کرایه در انتها
        self.assertEqual(payload['Items'][-1]['ErpCode'], 'SHIP-CUSTOM')

    def test_courier_order_with_zero_cost_has_no_shipping_row(self):
        order = self.order(shipping_cost=0, shipping_label='ارسال رایگان با پیک')
        self.assertEqual(self.shipping_rows(self.payload(order)), [])

    def test_postage_collect_order_never_gets_a_shipping_row_even_with_a_nonzero_cost(self):
        order = self.order(shipping_method='post', shipping_label='پس‌کرایه (پرداخت هزینه درب منزل)', zone='', shipping_cost=99999)
        payload = self.payload(order)
        self.assertEqual(self.shipping_rows(payload), [])
        self.assertEqual(payload['Items'], self.ITEMS)                              # فقط ردیف‌های کالا

    def test_legacy_order_without_method_keeps_the_old_rule_and_comment(self):
        legacy = self.order(shipping_method='', shipping_label='', province='', city='', zone='',
                            address='تهران، خیابان آزادی', shipping_cost=200000)
        self.assertEqual(self.shipping_rows(self.payload(legacy)),
                         [{'ErpCode': 'SHIP-1', 'Amount': 1, 'Price': 200000.0, 'Comment': 'هزینه ارسال و بسته‌بندی پستی'}])

    def test_legacy_order_with_zero_cost_has_no_shipping_row(self):
        legacy = self.order(shipping_method='', shipping_label='', province='', city='', zone='', shipping_cost=0)
        self.assertEqual(self.shipping_rows(self.payload(legacy)), [])

    def test_unknown_method_never_produces_a_shipping_row(self):
        self.assertEqual(self.shipping_rows(self.payload(self.order(shipping_method='drone'))), [])

    def test_courier_row_comment_without_a_zone(self):
        row = self.shipping_rows(self.payload(self.order(zone='', city='شیراز', province='فارس')))[0]
        self.assertEqual(row['Comment'], 'کرایه پیک (شیراز)')

    # ---------- آدرس کامل ----------
    def test_comment_carries_the_full_address_not_just_the_street(self):
        comment = self.payload(self.order())['Comment']
        self.assertIn('آدرس تحویل: قم، قم، پردیسان، بلوار پردیسان، فاز ۲', comment)
        self.assertIn('کد پستی: 3749113666', comment)
        self.assertIn('گیرنده: مریم کاظمی - 09123334455', comment)
        self.assertIn('سفارش آنلاین سایت کد #', comment)
        self.assertIn('ارسال: ارسال با پیک', comment)

    def test_postage_order_comment_says_the_postage_is_collected_on_delivery(self):
        order = self.order(shipping_method='post', shipping_label='پس‌کرایه (پرداخت هزینه درب منزل)', zone='', city='شیراز', province='فارس')
        comment = self.payload(order)['Comment']
        self.assertIn('آدرس تحویل: فارس، شیراز، بلوار پردیسان، فاز ۲', comment)
        self.assertIn('ارسال: پس‌کرایه (پرداخت هزینه درب منزل)', comment)

    def test_legacy_order_comment_uses_its_stored_full_text_address(self):
        legacy = self.order(shipping_method='', shipping_label='', province='', city='', zone='', address='تهران، خیابان آزادی، پلاک ۱')
        comment = self.payload(legacy)['Comment']
        self.assertIn('آدرس تحویل: تهران، خیابان آزادی، پلاک ۱', comment)
        self.assertNotIn('ارسال:', comment)                                         # سفارش قدیمی برچسب ارسال ندارد

    # ---------- ساختار کلی ----------
    def test_payload_structure(self):
        order = self.order()
        payload = self.payload(order)
        self.assertEqual(list(payload), ['CustomerErpCode', 'Date', 'Comment', 'Items'])
        self.assertEqual(payload['CustomerErpCode'], 'CUST-730')
        self.assertEqual(payload['Date'], order.created_at.strftime('%Y/%m/%d'))

    def test_deleted_user_or_unsynced_customer_falls_back_to_guest_code(self):
        self.assertEqual(self.payload(self.order(user=None))['CustomerErpCode'], 'GUEST_CODE')
        self.user.erp_code = None
        self.user.save(update_fields=['erp_code'])
        self.assertEqual(self.payload(self.order())['CustomerErpCode'], 'GUEST_CODE')


class OrderTaskInvoiceTests(TestCase):
    """ همان قواعد از مسیر واقعی تسک send_order_to_holoo (با کلاینت هلوی mock) """

    def setUp(self):
        self.user = CustomUser.objects.create_user(phone_number='09120000731', erp_code='CUST-731')
        category = Category.objects.create(name='تست', slug='holoo-invoice-cat')
        self.product = Product.objects.create(name='کالا', slug='holoo-invoice-product', erp_code='ERP-INV-1',
                                              category=category, price=100000, stock=5)

    def _send(self, **order_fields):
        data = dict(user=self.user, first_name='مریم', last_name='کاظمی', phone='09123334455', payment_method='cash',
                    address='بلوار پردیسان', total_price=100000, province='قم', city='قم', zone='پردیسان',
                    shipping_method='courier', shipping_label='ارسال با پیک', shipping_cost=45000)
        data.update(order_fields)
        order = Order.objects.create(**data)
        OrderItem.objects.create(order=order, product=self.product, price=100000, quantity=1)
        cache.delete('lock:holoo:invoice:%s' % order.id)
        with mock.patch('holoo.client.HolooClient.insert_invoice') as insert:
            insert.return_value = {'success': True, 'InvoiceCode': 'INV-X'}
            send_order_to_holoo(order.id)
        return insert.call_args[0][0]

    def test_courier_order_reaches_holoo_with_shipping_row_and_full_address(self):
        payload = self._send()
        codes = [row['ErpCode'] for row in payload['Items']]
        self.assertEqual(codes[0], 'ERP-INV-1')
        self.assertEqual(len(codes), 2)                                             # کالا + کرایه‌ی پیک
        self.assertEqual(payload['Items'][1]['Price'], 45000.0)
        self.assertIn('قم، قم، پردیسان، بلوار پردیسان', payload['Comment'])

    def test_postage_order_reaches_holoo_without_a_shipping_row(self):
        payload = self._send(shipping_method='post', shipping_label='پس‌کرایه (پرداخت هزینه درب منزل)', zone='', shipping_cost=0)
        self.assertEqual([row['ErpCode'] for row in payload['Items']], ['ERP-INV-1'])
        self.assertIn('ارسال: پس‌کرایه (پرداخت هزینه درب منزل)', payload['Comment'])

    def test_legacy_order_reaches_holoo_exactly_as_before(self):
        payload = self._send(shipping_method='', shipping_label='', province='', city='', zone='', address='تهران', shipping_cost=200000)
        self.assertEqual([row['Comment'] for row in payload['Items']][1:], ['هزینه ارسال و بسته‌بندی پستی'])
        self.assertEqual(payload['Items'][1]['Price'], 200000.0)
