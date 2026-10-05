"""
تست اصلاحات سرور: موجودی قابل‌فروش min(Few, FewSpd)، تلاش مجدد سریع OTP، هشدار حالت mock در ادمین سفارش‌ها،
و اجرای بی‌خطای فرمان فقط‌خواندنیِ diagnose_server.
"""
from io import StringIO
from unittest import mock

from django.contrib.admin.sites import site
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.management import call_command
from django.test import RequestFactory, SimpleTestCase, TestCase

from holoo.product_state import row_values, sellable_stock
from notifications.tasks import FAST_RETRY_COUNTDOWNS, retry_countdown
from orders.models import Order


class SellableStockTests(SimpleTestCase):
    def test_uses_fewspd_when_lower(self):
        self.assertEqual(sellable_stock({'Few': 37, 'FewSpd': 35}), 35)

    def test_uses_few_when_fewspd_missing(self):
        self.assertEqual(sellable_stock({'Few': 37}), 37)
        self.assertEqual(sellable_stock({'Few': 37, 'FewSpd': None}), 37)

    def test_never_above_few(self):
        self.assertEqual(sellable_stock({'Few': 10, 'FewSpd': 50}), 10)

    def test_never_negative(self):
        self.assertEqual(sellable_stock({'Few': 3, 'FewSpd': -2}), 0)
        self.assertEqual(sellable_stock({'Few': -1}), 0)

    def test_garbage_fewspd_falls_back_to_few(self):
        self.assertEqual(sellable_stock({'Few': 7, 'FewSpd': 'abc'}), 7)

    def test_row_values_stock_uses_sellable(self):
        self.assertEqual(row_values({'Few': 37, 'FewSpd': 35, 'SellPrice': 1000})['stock'], 35)


class RetryCountdownTests(SimpleTestCase):
    def test_otp_retries_quickly(self):
        self.assertEqual([retry_countdown('otp', n) for n in range(3)], [3, 6, 12])

    def test_otp_capped_at_last_step(self):
        self.assertEqual(retry_countdown('otp', 50), FAST_RETRY_COUNTDOWNS[-1])

    def test_other_templates_keep_exponential_backoff(self):
        self.assertEqual(retry_countdown('order_registered', 0), 60)
        self.assertEqual(retry_countdown('order_registered', 2), 240)
        self.assertEqual(retry_countdown('order_registered', 20), 3600)


class OrderAdminWriteWarningTests(TestCase):
    def setUp(self):
        self.admin = site._registry[Order]
        self.user = get_user_model().objects.create_superuser('09120000099', password='x')

    def _request(self):
        request = RequestFactory().get('/admin/orders/order/')
        request.user = self.user
        request.session = {}
        request._messages = FallbackStorage(request)
        return request

    def test_no_warning_when_real(self):
        with mock.patch('holoo.conf.get_config') as get:
            get.return_value.write_is_real = True
            self.assertIsNone(self.admin._holoo_write_warning())

    def test_warning_when_mock(self):
        with mock.patch('holoo.conf.get_config') as get:
            get.return_value.write_is_real = False
            get.return_value.write_is_mock = True
            self.assertIn('HOLOO_WRITE_MODE', self.admin._holoo_write_warning())

    def test_warning_when_disabled(self):
        with mock.patch('holoo.conf.get_config') as get:
            get.return_value.write_is_real = False
            get.return_value.write_is_mock = False
            self.assertIn('غیرفعال', self.admin._holoo_write_warning())


class DiagnoseServerCommandTests(TestCase):
    def test_runs_read_only_without_crashing(self):
        out = StringIO()
        with mock.patch('config.celery.app.control.inspect') as inspect:
            inspect.return_value.ping.return_value = {}
            call_command('diagnose_server', stdout=out)
        text = out.getvalue()
        self.assertIn('== محیط ==', text)
        self.assertIn('== جمع‌بندی ==', text)


class RequeueMockOrdersTests(TestCase):
    """ پاک‌سازی سفارش/مشتریِ ساختگیِ حالت mock و ارسال دوباره (فقط با --apply و فقط در حالت real) """

    def setUp(self):
        from accounts.models import CustomUser
        self.user = CustomUser.objects.create_user(phone_number='09120000777', erp_code='ERP_0777')
        self.mock_order = Order.objects.create(user=self.user, first_name='a', last_name='b', phone='09120000777', address='x',
                                               payment_method='cash', total_price=1, approved_at='2026-10-05T10:00:00Z',
                                               holoo_invoice_id='INV_12345', holoo_receipt_id='RCP_12345')
        self.real_order = Order.objects.create(user=self.user, first_name='a', last_name='b', phone='09120000777', address='x',
                                               payment_method='cash', total_price=1, approved_at='2026-10-05T10:00:00Z',
                                               holoo_invoice_id='36710')

    def run_cmd(self, *args, real=True):
        out = StringIO()
        with mock.patch('holoo.management.commands.requeue_mock_orders.get_config') as get, \
                mock.patch('holoo.tasks.send_order_to_holoo.delay') as delay:
            get.return_value.write_is_real = real
            get.return_value.write_mode = 'real' if real else 'mock'
            get.return_value.db_name = 'Holoo2'
            with self.captureOnCommitCallbacks(execute=True):
                call_command('requeue_mock_orders', *args, stdout=out)
        return out.getvalue(), delay

    def test_dry_run_changes_nothing(self):
        text, delay = self.run_cmd()
        self.assertIn('--apply', text)
        delay.assert_not_called()
        self.assertEqual(Order.objects.get(pk=self.mock_order.pk).holoo_invoice_id, 'INV_12345')

    def test_apply_clears_only_mock_artifacts_and_requeues(self):
        _, delay = self.run_cmd('--apply')
        order = Order.objects.get(pk=self.mock_order.pk)
        self.assertIsNone(order.holoo_invoice_id)
        self.assertIsNone(order.holoo_receipt_id)
        self.user.refresh_from_db()
        self.assertIsNone(self.user.erp_code)
        self.assertEqual(Order.objects.get(pk=self.real_order.pk).holoo_invoice_id, '36710')
        delay.assert_called_once_with(self.mock_order.pk)

    def test_apply_is_refused_when_not_real(self):
        from django.core.management import CommandError
        with self.assertRaises(CommandError):
            self.run_cmd('--apply', real=False)
        self.assertEqual(Order.objects.get(pk=self.mock_order.pk).holoo_invoice_id, 'INV_12345')
