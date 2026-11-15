"""
بازبینی اپ holoo (۱۴ نوامبر ۲۰۲۶): حذف کالا (mock/پاسخ ناقص/تلرانس)، کامل‌بودن فاکتور و مبلغ، بازیابی فاکتور قبلی،
غیرفعال‌شدن قطعی هنگام استعلام موجودی، نوشتن اتمیک وضعیت کالا، قفل با مالک، و وصل‌شدن به مشتریِ موجود در هلو.
هیچ تماسی به هلو نیست.
"""
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from accounts.models import CustomUser
from holoo import stock_refresh as holoo_refresh
from holoo.client import HolooClient
from holoo.conf import get_config
from holoo.invoice import build_return_payload, payload_total, return_item_lines
from holoo.locks import task_lock
from holoo.product_state import apply_holoo_product_state
from holoo.tasks import send_order_to_holoo, sync_customer, sync_products_from_holoo
from holoo.tests_real_write import real_config, response
from holoo.tests_return_invoice import ReturnHolooBase
from orders.models import Order, OrderItem
from products.models import Category, Product


def holoo_row(erp, few=5, active=True, name='کالا'):
    return {'ErpCode': erp, 'Name': name, 'Code': erp[-3:], 'Few': few, 'SellPrice': 100000, 'IsActive': active,
            **{f'SellPrice{i}': 0 for i in range(2, 11)}}


class ConfigTests(SimpleTestCase):
    def test_an_invalid_read_mode_is_flagged_not_silently_trusted(self):
        config = get_config(env={'HOLOO_READ_MODE': 'mokc'}, env_file='does-not-exist.env')
        self.assertTrue(config.read_invalid)
        self.assertTrue(config.read_is_mock)

    def test_valid_modes_are_not_flagged(self):
        for mode in ('real', 'mock'):
            self.assertFalse(get_config(env={'HOLOO_READ_MODE': mode}, env_file='does-not-exist.env').read_invalid)


class ProductPruneSafetyTests(TestCase):
    """ سینک نباید کالای واقعی را به‌خاطر mock، پاسخ ناقص یا تلرانس پاک کند """

    def setUp(self):
        category = Category.objects.create(name='تست', slug='nov14-prune-cat')
        self.products = {
            erp: Product.objects.create(name=f'کالا {erp}', slug=f'nov14-{erp.lower()}', erp_code=erp, category=category,
                                        price=100000, stock=5)
            for erp in ('ERP-N1', 'ERP-N2', 'ERP-N3')
        }
        cache.delete('lock:holoo:product_sync')

    def sync(self, items, count, env=None, **patch_kwargs):
        env = env or {}
        with mock.patch.dict('os.environ', env), \
             mock.patch('holoo.client.HolooClient.get_product_count', return_value=count), \
             mock.patch('holoo.client.HolooClient.get_products', **patch_kwargs or {'return_value': {'product': items}}) as get_products:
            result = sync_products_from_holoo()
        return result, get_products

    def alive(self):
        return set(Product.objects.filter(erp_code__in=self.products).values_list('erp_code', flat=True))

    def test_an_invalid_read_mode_refuses_the_sync_and_keeps_the_catalog(self):
        result, _ = self.sync([], 0, env={'HOLOO_READ_MODE': 'mokc'})
        self.assertTrue(str(result).startswith('Refused'))
        self.assertEqual(self.alive(), set(self.products))

    def test_mock_mode_never_prunes_the_real_catalog(self):
        self.sync([], 0, env={'HOLOO_READ_MODE': 'mock'})
        self.assertEqual(self.alive(), set(self.products))

    def test_a_failure_response_is_a_failed_fetch_not_an_empty_catalog(self):
        self.sync(None, 3, return_value={'Failure': {'ErrorCode': 9, 'Error': 'boom'}})
        self.assertEqual(self.alive(), set(self.products))

    def test_a_short_fetch_is_not_pruned_even_when_it_is_within_five_products(self):
        self.sync([holoo_row('ERP-N1'), holoo_row('ERP-N2')], 3)       # هلو ۳ کالا گزارش داده ولی ۲ ردیف آمد
        self.assertEqual(self.alive(), set(self.products))

    def test_a_complete_fetch_still_prunes_what_vanished_from_holoo(self):
        self.sync([holoo_row('ERP-N1'), holoo_row('ERP-N2')], 2)
        self.assertEqual(self.alive(), {'ERP-N1', 'ERP-N2'})

    def test_rows_without_erp_code_count_toward_completeness(self):
        self.sync([holoo_row('ERP-N1'), holoo_row('ERP-N2'), {'Name': 'بی‌کد', 'IsActive': True}], 3)
        self.assertEqual(self.alive(), {'ERP-N1', 'ERP-N2'})

    def test_a_product_that_errors_during_sync_blocks_the_prune(self):
        with mock.patch('holoo.tasks.apply_holoo_product_state', side_effect=RuntimeError('db')):
            self.sync([holoo_row('ERP-N1'), holoo_row('ERP-N2')], 2)
        self.assertEqual(self.alive(), set(self.products))

    def test_the_page_after_the_last_one_is_not_requested(self):
        with mock.patch('holoo.tasks.PRODUCT_SYNC_PAGE_SIZE', 2):
            _, get_products = self.sync([holoo_row('ERP-N1'), holoo_row('ERP-N2')], 2)
        self.assertEqual(get_products.call_count, 1)


class InvoiceCompletenessTests(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(phone_number='09120000914', erp_code='CUST-914')
        category = Category.objects.create(name='تست', slug='nov14-invoice-cat')
        self.a = Product.objects.create(name='الف', slug='nov14-inv-a', erp_code='ERP-IA', category=category, price=100000, stock=9)
        patcher = mock.patch('notifications.tasks.deliver_notification.delay')
        patcher.start()
        self.addCleanup(patcher.stop)

    def order(self, total, *, with_orphan_line=False):
        order = Order.objects.create(
            approved_at=timezone.now(), user=self.user, first_name='مریم', last_name='کاظمی', phone='09123334455',
            payment_method='check', address='بلوار', province='قم', city='قم', zone='پردیسان', shipping_method='post',
            shipping_cost=0, total_price=total,
        )
        OrderItem.objects.create(order=order, product=self.a, price=100000, quantity=1)
        if with_orphan_line:
            OrderItem.objects.create(order=order, product=None, price=200000, quantity=1)   # کالایش بعداً از سایت حذف شده
        cache.delete('lock:holoo:invoice:%s' % order.id)
        return order

    def send(self, order):
        with mock.patch('holoo.client.HolooClient.insert_invoice') as insert:
            insert.return_value = {'success': True, 'InvoiceCode': 'INV-N14'}
            outcome = send_order_to_holoo(order.id)
        order.refresh_from_db()
        return outcome, insert

    def test_an_order_with_a_line_whose_product_is_gone_is_flagged_and_not_sent(self):
        order = self.order(300000, with_orphan_line=True)
        outcome, insert = self.send(order)
        insert.assert_not_called()
        self.assertTrue(order.holoo_needs_attention)
        self.assertFalse(order.holoo_invoice_id)
        self.assertIn('قلم', order.holoo_last_error)
        self.assertTrue(str(outcome).startswith('Needs attention'))

    def test_a_complete_matching_order_is_still_sent(self):
        order = self.order(100000)
        _, insert = self.send(order)
        insert.assert_called_once()
        self.assertEqual(order.holoo_invoice_id, 'INV-N14')
        self.assertFalse(order.holoo_needs_attention)

    def test_an_amount_mismatch_blocks_the_invoice_and_flags_the_order(self):
        order = self.order(999)                  # مبلغ سفارش با جمع ردیف‌ها (۱۰۰٬۰۰۰) نمی‌خواند
        with self.assertLogs('holoo.tasks', level='ERROR'):
            _, insert = self.send(order)
        insert.assert_not_called()
        self.assertTrue(order.holoo_needs_attention)
        self.assertIn('مغایرت مبلغ', order.holoo_last_error)


class ReturnMismatchTests(ReturnHolooBase):
    def test_a_return_whose_invoice_total_differs_from_the_refund_is_flagged_not_sent(self):
        request = self.to_refund_pending(quantity=2)
        with self.assertLogs('holoo.tasks', level='ERROR'),              mock.patch('returns.models.ReturnRequest.total_refund_amount', new_callable=mock.PropertyMock, return_value=12345):
            _, insert = self.run_task(request)
        insert.assert_not_called()
        request.refresh_from_db()
        self.assertTrue(request.holoo_needs_attention)
        self.assertFalse(request.holoo_invoice_id)


class InvoiceRecoveryTests(ReturnHolooBase):
    """ بعد از خطای ۱۰۲ فقط فاکتورِ هم‌مبلغ و بی‌ابهام پذیرفته می‌شود """

    def setUp(self):
        super().setUp()
        self.request_obj = self.to_refund_pending()
        self.the_payload = build_return_payload(self.request_obj, return_item_lines(self.request_obj)[0], '999999')
        self.expected = float(payload_total(self.the_payload))

    def duplicate(self, rows):
        request, payload = self.request_obj, self.the_payload
        client = HolooClient(real_config())
        failure = response({'Header': 'Invoice', 'Failure': {'ErrorCode': 102, 'Error': 'duplicate'}})
        listing = {'invoice': [{'Code': code, 'ErpCode': f'E{code}=', 'CustomerErpCode': 'CUST=', 'SumPrice': total,
                                'comment': f'برگشت از فروش سايت - مرجوعي #{request.pk} - سفارش #1'} for code, total in rows]}
        with mock.patch('holoo.client.requests.post', return_value=failure), \
             mock.patch.object(HolooClient, '_get_auth_header', return_value='Bearer t'), \
             mock.patch.object(HolooClient, 'get_json', return_value=listing):
            return client.insert_invoice(payload)

    def test_an_existing_invoice_with_another_amount_is_not_adopted(self):
        result = self.duplicate([(1762, 999.0)])
        self.assertFalse(result['success'])
        self.assertIs(result['transient'], False)            # وگرنه سفارش (max_retries=None) تا ابد دوباره تلاش می‌شود

    def test_two_invoices_with_the_exact_amount_are_ambiguous_and_not_adopted(self):
        result = self.duplicate([(1762, self.expected), (1763, self.expected)])
        self.assertFalse(result['success'])
        self.assertIs(result['transient'], False)

    def test_the_single_exact_invoice_is_adopted(self):
        result = self.duplicate([(1762, self.expected), (1763, 999.0)])
        self.assertEqual((result['success'], result['adopted'], result['InvoiceCode']), (True, True, '1762'))

    def test_helper_without_an_expected_total_still_requires_exactly_one_row(self):
        from holoo.client import _single_matching_invoice
        self.assertIsNone(_single_matching_invoice([{'Code': 1}, {'Code': 2}], None, 'x'))
        self.assertEqual(_single_matching_invoice([{'Code': 1}], None, 'x')['Code'], 1)


class StockRefreshInactiveTests(TestCase):
    def setUp(self):
        patcher = mock.patch.object(holoo_refresh, 'TEST_RUN', False)
        patcher.start()
        self.addCleanup(patcher.stop)
        category = Category.objects.create(name='تست', slug='nov14-refresh-cat')
        self.product = Product.objects.create(name='الف', slug='nov14-refresh-a', erp_code='ERP-RF-X', product_code='001',
                                              category=category, price=100000, stock=9)

    def run_provider(self, answer):
        client = mock.Mock()
        client.config.read_is_mock = False
        client.get_json.return_value = answer
        with mock.patch.object(holoo_refresh, 'HolooClient', return_value=client), \
             mock.patch('notifications.tasks.deliver_notification.delay'), self.captureOnCommitCallbacks(execute=True):
            return holoo_refresh.refresh_products([self.product])

    def test_a_valid_inactive_answer_deactivates_the_product_instead_of_counting_as_a_failure(self):
        result = self.run_provider({'product': [holoo_row('ERP-RF-X', few=0, active=False)]})
        self.assertEqual(result, {'refreshed': 1, 'failed': 0})
        self.product.refresh_from_db()
        self.assertFalse(self.product.is_active)
        self.assertEqual(self.product.stock, 0)

    def test_a_missing_row_is_still_just_a_failed_lookup(self):
        result = self.run_provider({'product': []})
        self.assertEqual(result, {'refreshed': 0, 'failed': 1})
        self.product.refresh_from_db()
        self.assertTrue(self.product.is_active)
        self.assertEqual(self.product.stock, 9)


class AtomicStateWriteTests(TestCase):
    def test_a_stale_in_memory_product_cannot_overwrite_newer_database_data(self):
        category = Category.objects.create(name='تست', slug='nov14-state-cat')
        product = Product.objects.create(name='الف', slug='nov14-state-a', erp_code='ERP-ST', category=category, price=100000, stock=7)
        stale = Product.objects.get(pk=product.pk)                      # نسخه‌ی حافظه: هنوز stock_synced_at ندارد
        newest = timezone.now()
        Product.objects.filter(pk=product.pk).update(stock=1, stock_synced_at=newest)    # واکشیِ هم‌زمانِ جدیدتر نوشت
        applied = apply_holoo_product_state(stale, holoo_row('ERP-ST', few=10), newest - timedelta(seconds=5))
        self.assertFalse(applied)
        product.refresh_from_db()
        self.assertEqual(product.stock, 1)

    def test_a_newer_observation_still_writes(self):
        category = Category.objects.create(name='تست', slug='nov14-state-cat2')
        product = Product.objects.create(name='الف', slug='nov14-state-b', erp_code='ERP-ST2', category=category, price=100000, stock=7)
        older = timezone.now() - timedelta(minutes=5)
        Product.objects.filter(pk=product.pk).update(stock_synced_at=older)
        self.assertTrue(apply_holoo_product_state(Product.objects.get(pk=product.pk), holoo_row('ERP-ST2', few=4), timezone.now()))
        product.refresh_from_db()
        self.assertEqual(product.stock, 4)


class OwnedLockTests(TestCase):
    def setUp(self):
        cache.delete('lock:nov14-owner')

    def test_a_finished_run_whose_lock_expired_does_not_release_the_next_runs_lock(self):
        first = task_lock('nov14-owner', timeout=30)
        self.assertTrue(first.__enter__())
        cache.delete('lock:nov14-owner')                              # قفل اجرای اول منقضی شد
        second = task_lock('nov14-owner', timeout=30)
        self.assertTrue(second.__enter__())                           # اجرای دوم قفل گرفت
        first.__exit__(None, None, None)                              # اجرای اول تمام شد
        with task_lock('nov14-owner', timeout=30) as third:
            self.assertFalse(third)                                   # اجرای سوم هنوز نباید وارد شود
        second.__exit__(None, None, None)


class AdoptedCustomerTests(TestCase):
    def test_a_customer_adopted_by_phone_is_not_overwritten_on_the_next_sync(self):
        user = CustomUser.objects.create_user(phone_number='09120000915', first_name='مریم', last_name='کاظمی')
        client = mock.MagicMock()
        client.config.write_is_mock = False
        client.insert_person.return_value = {'success': True, 'erp_code': 'ERP-C1', 'code': '77', 'bed_sarfasl': '1030',
                                             'adopted': True, 'holoo_owned': True}
        self.assertTrue(sync_customer(user, client)['success'])
        user.refresh_from_db()
        self.assertTrue(user.holoo_owns_customer)
        self.assertFalse(user.imported_from_holoo)                    # تأیید خودکار و سطح قیمت فقط برای واردشده‌ی گروهی است

        client._lookup_customer.return_value = {'Name': 'نام در هلو', 'WebId': '', 'NationalId': '1', 'Mobile': '0912', 'Address': 'آدرس'}
        sync_customer(user, client)
        client.update_person.assert_not_called()                      # هلو مالک است؛ چیزی بازنویسی نمی‌شود

    def test_a_normal_new_customer_is_not_marked_as_holoo_owned(self):
        user = CustomUser.objects.create_user(phone_number='09120000916')
        client = mock.MagicMock()
        client.config.write_is_mock = False
        client.insert_person.return_value = {'success': True, 'erp_code': 'ERP-C2', 'code': '78', 'bed_sarfasl': '1030', 'adopted': False}
        sync_customer(user, client)
        user.refresh_from_db()
        self.assertFalse(user.holoo_owns_customer)
