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
