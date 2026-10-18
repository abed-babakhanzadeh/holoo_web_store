"""
تازه‌سازی موجودی لحظه‌ی خرید (products/stock_refresh.py + holoo/stock_refresh.py): ثبت‌نام تأمین‌کننده، هم‌زمانی و سقف زمانی، خطاها
بی‌اثر بر خرید، و اتصال به ثبت سفارش و شروع پرداخت. هیچ تماسی به هلو واقعی نیست.
"""
import time
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from accounts.testing import make_approved_user
from holoo import stock_refresh as holoo_refresh
from notifications.models import Notification
from orders.models import Order
from orders.tests_stock_flow import StockFlowBase
from products import stock_refresh
from products.models import Category, Product, SiteSettings, StockAlert

from django.urls import reverse


def row(erp, few, spd=None):
    item = {'ErpCode': erp, 'Name': 'کالا', 'Code': '1', 'Few': few, 'SellPrice': 100000, 'IsActive': True,
            **{f'SellPrice{i}': 0 for i in range(2, 11)}}
    if spd is not None:
        item['FewSpd'] = spd
    return item


class ProviderTests(TestCase):
    def setUp(self):
        patcher = mock.patch.object(holoo_refresh, 'TEST_RUN', False)
        patcher.start()
        self.addCleanup(patcher.stop)
        category = Category.objects.create(name='تست', slug='refresh-cat')
        self.a = Product.objects.create(name='الف', slug='refresh-a', erp_code='ERP-RF-A', product_code='001', category=category,
                                        price=100000, stock=9)
        self.b = Product.objects.create(name='ب', slug='refresh-b', erp_code='ERP-RF-B', product_code='002', category=category,
                                        price=100000, stock=9)
        self.nocode = Product.objects.create(name='بی‌کد', slug='refresh-n', erp_code='ERP-RF-N', product_code=None,
                                             category=category, price=100000, stock=9)

    def run_provider(self, answers, products=None, read_is_mock=False):
        client = mock.Mock()
        client.config.read_is_mock = read_is_mock

        def get_json(path, params=None, timeout=None):
            return answers(params['code']) if callable(answers) else answers.get(params['code'])
        client.get_json.side_effect = get_json
        with mock.patch.object(holoo_refresh, 'HolooClient', return_value=client), \
             mock.patch('notifications.tasks.deliver_notification.delay'), self.captureOnCommitCallbacks(execute=True):
            result = holoo_refresh.refresh_products(products or [self.a, self.b, self.nocode])
        return result, client

    def test_fresh_stock_is_written_for_every_product_with_a_code(self):
        result, client = self.run_provider({'001': {'product': [row('ERP-RF-A', 3, 3)]}, '002': {'product': [row('ERP-RF-B', 0)]}})
        self.assertEqual(result, {'refreshed': 2, 'failed': 0})
        self.a.refresh_from_db()
        self.b.refresh_from_db()
        self.assertEqual((self.a.stock, self.b.stock), (3, 0))
        queried = sorted(call.kwargs['params']['code'] for call in client.get_json.call_args_list)
        self.assertEqual(queried, ['001', '002'])                      # کالای بی‌کد پرسیده نمی‌شود

    def test_sellable_column_is_used_not_the_raw_stock(self):
        self.run_provider({'001': {'product': [row('ERP-RF-A', 10, 4)]}}, products=[self.a])
        self.a.refresh_from_db()
        self.assertEqual(self.a.stock, 4)

    def test_holoo_down_or_unknown_answers_leave_the_stock_untouched(self):
        result, _ = self.run_provider({'001': None, '002': {'product': []}})
        self.assertEqual(result, {'refreshed': 0, 'failed': 2})
        self.a.refresh_from_db()
        self.assertEqual(self.a.stock, 9)

    def test_one_failure_does_not_block_the_others(self):
        result, _ = self.run_provider({'001': None, '002': {'product': [row('ERP-RF-B', 1)]}})
        self.assertEqual(result, {'refreshed': 1, 'failed': 1})
        self.b.refresh_from_db()
        self.assertEqual(self.b.stock, 1)

    def test_slow_holoo_is_cut_off_by_the_total_budget(self):
        def slow(code):
            time.sleep(0.4)
            return {'product': [row('ERP-RF-A', 1)]}
        with mock.patch.object(holoo_refresh, 'TOTAL_BUDGET', 0.05):
            started = time.monotonic()
            result, _ = self.run_provider(slow, products=[self.a])
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.35)
        self.assertEqual((result['refreshed'], result['failed']), (0, 1))
        self.a.refresh_from_db()
        self.assertEqual(self.a.stock, 9)

    def test_mock_mode_and_test_runs_never_call_holoo(self):
        result, client = self.run_provider({}, read_is_mock=True)
        self.assertEqual(result['skipped'], 'mock')
        client.get_json.assert_not_called()
        with mock.patch.object(holoo_refresh, 'TEST_RUN', True), mock.patch.object(holoo_refresh, 'HolooClient') as cls:
            self.assertEqual(holoo_refresh.refresh_products([self.a])['skipped'], 'test run')
        cls.assert_not_called()

    def test_stock_that_appears_triggers_the_waiting_customers_sms(self):
        Product.objects.filter(pk=self.a.pk).update(stock=0)
        user = make_approved_user('09125550303')
        StockAlert.objects.create(product=self.a, user=user, channel=StockAlert.CHANNEL_SMS)
        self.a.refresh_from_db()
        self.run_provider({'001': {'product': [row('ERP-RF-A', 10, 10)]}}, products=[self.a])
        self.assertEqual(Notification.objects.filter(template_key='back_in_stock_sms', recipient=user.phone_number).count(), 1)


class RegistryTests(SimpleTestCase):
    def test_nothing_registered_or_disabled_means_no_call_and_errors_are_swallowed(self):
        calls = []
        with mock.patch.object(stock_refresh, '_refresher', lambda products: calls.append(products) or {'refreshed': 1}):
            with mock.patch.object(stock_refresh, 'enabled', return_value=True):
                self.assertEqual(stock_refresh.refresh_products([object()]), {'refreshed': 1})
            with mock.patch.object(stock_refresh, 'enabled', return_value=False):
                self.assertIsNone(stock_refresh.refresh_products([object()]))
            self.assertEqual(len(calls), 1)
        with mock.patch.object(stock_refresh, '_refresher', None):
            self.assertIsNone(stock_refresh.refresh_products([object()]))

        def boom(products):
            raise RuntimeError('holoo exploded')
        with mock.patch.object(stock_refresh, '_refresher', boom), mock.patch.object(stock_refresh, 'enabled', return_value=True):
            self.assertIsNone(stock_refresh.refresh_products([object()]))                       # استثنا بیرون نمی‌زند


class CheckoutIntegrationTests(StockFlowBase):
    """ ثبت سفارش و شروع پرداخت با تأمین‌کننده‌ی جعلی (به‌جای هلو) """

    def lowering(self, new_stock):
        def refresher(products):
            Product.objects.filter(pk__in=[p.pk for p in products]).update(stock=new_stock)
            return {'refreshed': len(products), 'failed': 0}
        return refresher

    def test_stock_sold_out_in_holoo_since_the_last_sync_blocks_the_order(self):
        refresher = mock.Mock(side_effect=self.lowering(1))
        with mock.patch.object(stock_refresh, '_refresher', refresher):
            response = self.submit('check')
        self.assertContains(response, 'حداکثر 1 عدد', status_code=409)
        self.assertEqual(Order.objects.count(), 0)
        refresher.assert_called_once()
        self.assertEqual([p.pk for p in refresher.call_args.args[0]], [self.product.pk])        # فقط کالاهای همین سبد

    def test_unchanged_stock_places_the_order_as_before(self):
        refresher = mock.Mock(return_value={'refreshed': 1, 'failed': 0})
        with mock.patch.object(stock_refresh, '_refresher', refresher):
            self.assertEqual(self.submit('check').status_code, 302)
        self.assertEqual(Order.objects.count(), 1)

    def test_a_failing_refresher_never_blocks_the_purchase(self):
        with mock.patch.object(stock_refresh, '_refresher', mock.Mock(side_effect=RuntimeError('holoo down'))):
            self.assertEqual(self.submit('check').status_code, 302)
        self.assertEqual(Order.objects.count(), 1)

    def test_the_site_setting_can_turn_the_check_off(self):
        SiteSettings.objects.update_or_create(pk=1, defaults={'verify_stock_at_checkout': False})
        cache.delete(SiteSettings.CACHE_KEY)
        self.addCleanup(cache.delete, SiteSettings.CACHE_KEY)
        refresher = mock.Mock(return_value={})
        with mock.patch.object(stock_refresh, '_refresher', refresher):
            self.assertEqual(self.submit('check').status_code, 302)
        refresher.assert_not_called()
        self.assertTrue(SiteSettings._meta.get_field('verify_stock_at_checkout').default)       # پیش‌فرض روشن

    def test_payment_start_refreshes_the_orders_products_and_cancels_when_they_ran_out(self):
        order = self._place_online_order()
        refresher = mock.Mock(side_effect=self.lowering(1))
        with mock.patch.object(stock_refresh, '_refresher', refresher):
            response = self.client.post(reverse('payments:start_payment', args=[order.id]), {'wallet_amount': '0'})
        self.assertRedirects(response, reverse('orders:order_detail_full', args=[order.id]), fetch_redirect_response=False)
        order.refresh_from_db()
        self.assertEqual(order.status, 'canceled')
        self.assertEqual([p.pk for p in refresher.call_args.args[0]], [self.product.pk])

    def test_payment_start_proceeds_when_stock_is_still_enough(self):
        order = self._place_online_order()
        refresher = mock.Mock(side_effect=self.lowering(10))
        with mock.patch.object(stock_refresh, '_refresher', refresher):
            response = self.client.post(reverse('payments:start_payment', args=[order.id]), {'wallet_amount': '0'})
        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertNotEqual(order.status, 'canceled')
        refresher.assert_called_once()

    def _place_online_order(self):
        self.assertEqual(self.submit('cash').status_code, 302)
        return Order.objects.get(user=self.user)
