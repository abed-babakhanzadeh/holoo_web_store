"""
پایش اتصال هلو (holoo/health.py): هشدار قطعی پس از آستانه، یادآوری، پیام وصل‌شدن، یادداشت خطای موقت روی سفارش و بنرهای ادمین.
به هلو و پیامک واقعی نمی‌رود.
"""
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomUser
from holoo import health
from holoo.tasks import check_holoo_health, send_order_to_holoo
from holoo.tests_real_write import TaskBase
from notifications.models import Notification
from orders.models import Order


class FakeClient:
    def __init__(self, up=True, mock_mode=False):
        self.up = up
        self.config = mock.Mock(read_is_mock=mock_mode)

    def get_json(self, path, params=None, timeout=None):
        return {'version': '1.7.33'} if self.up else None


class HealthCheckTests(TestCase):
    def setUp(self):
        cache.delete_many([health.DOWN_SINCE_KEY, health.LAST_ALERT_KEY])
        self.addCleanup(cache.delete_many, [health.DOWN_SINCE_KEY, health.LAST_ALERT_KEY])
        self.t0 = timezone.now()

    def run_check(self, up, minutes=0):
        with mock.patch('notifications.tasks.deliver_notification.delay'), self.captureOnCommitCallbacks(execute=True):
            return health.check_holoo_health(FakeClient(up=up), now=self.t0 + timedelta(minutes=minutes))

    def alerts(self, key):
        return Notification.objects.filter(template_key=key).count()

    def test_a_healthy_holoo_does_nothing(self):
        self.assertEqual(self.run_check(True), 'ok')
        self.assertEqual(Notification.objects.count(), 0)
        self.assertIsNone(health.current_outage())

    def test_mock_read_mode_is_skipped(self):
        result = health.check_holoo_health(FakeClient(up=False, mock_mode=True))
        self.assertTrue(result.startswith('skipped'))
        self.assertIsNone(health.current_outage())

    def test_short_outage_does_not_alert_but_is_tracked(self):
        self.run_check(False, 0)
        result = self.run_check(False, 5)
        self.assertIn('below alert threshold', result)
        self.assertEqual(self.alerts('holoo_down_admin'), 0)
        self.assertIsNotNone(health.current_outage())

    def test_alert_after_the_threshold_once_then_reminder_after_hours(self):
        self.run_check(False, 0)
        result = self.run_check(False, 11)
        self.assertIn('alert sent', result)
        self.assertEqual(self.alerts('holoo_down_admin'), 1)
        self.run_check(False, 20)                                   # هشدار تکراری نمی‌رود
        self.run_check(False, 120)
        self.assertEqual(self.alerts('holoo_down_admin'), 1)
        self.run_check(False, 11 + 181)                             # ۳ ساعت از آخرین هشدار
        self.assertEqual(self.alerts('holoo_down_admin'), 2)

    def test_recovery_message_only_after_an_alert(self):
        self.run_check(False, 0)
        self.assertEqual(self.run_check(True, 3), 'ok')            # قبل از هشدار: پیام وصل‌شدن نمی‌رود
        self.assertEqual(self.alerts('holoo_recovered_admin'), 0)
        self.run_check(False, 10)
        self.run_check(False, 25)
        self.assertEqual(self.alerts('holoo_down_admin'), 1)
        result = self.run_check(True, 40)
        self.assertTrue(result.startswith('recovered'))
        self.assertEqual(self.alerts('holoo_recovered_admin'), 1)
        self.assertIsNone(health.current_outage())
        self.assertEqual(self.run_check(True, 45), 'ok')           # دوباره نمی‌فرستد

    def test_alert_text_reports_pending_orders_and_goes_to_admin_numbers(self):
        from products.models import SiteSettings
        SiteSettings.objects.update_or_create(pk=1, defaults={'store_admin_sms_recipient': '09127540293'})
        cache.delete(SiteSettings.CACHE_KEY)
        self.addCleanup(cache.delete, SiteSettings.CACHE_KEY)
        self.run_check(False, 0)
        self.run_check(False, 12)
        note = Notification.objects.get(template_key='holoo_down_admin')
        self.assertEqual(note.recipient, '09127540293')
        self.assertIn('TNCHolooWebService', note.text)
        self.assertIn('12', note.text)

    def test_celery_task_delegates(self):
        with mock.patch('holoo.health.check_holoo_health', return_value='ok') as run:
            self.assertEqual(check_holoo_health(), 'ok')
        run.assert_called_once()


class AttemptErrorNoteTests(TaskBase):
    def setUp(self):
        super().setUp()
        CustomUser.objects.filter(pk=self.user.pk).update(erp_code='CUST=')

    def test_transient_failure_leaves_a_visible_note_without_flagging_the_order(self):
        order = self.make_order(paid=False, method='check')
        failure = {'success': False, 'code': 'NETWORK', 'transient': True, 'message': 'Connection refused 127.0.0.1:8080'}
        with mock.patch('holoo.client.HolooClient.insert_invoice', return_value=failure), \
             mock.patch('holoo.tasks.send_order_to_holoo.retry', side_effect=RuntimeError('retried')):
            with self.assertRaises(RuntimeError):
                send_order_to_holoo.run(order.id)
        order.refresh_from_db()
        self.assertFalse(order.holoo_needs_attention)
        self.assertIn('Connection refused', order.holoo_last_error)
        self.assertIn('موقت', order.holoo_last_error)
        self.assertEqual(order.accounting_state[0], Order.ACCOUNTING_QUEUED)

    def test_system_exception_is_noted_too_and_success_clears_it(self):
        order = self.make_order(paid=False, method='check')
        with mock.patch('holoo.client.HolooClient.insert_invoice', side_effect=OSError('socket down')), \
             mock.patch('holoo.tasks.send_order_to_holoo.retry', side_effect=RuntimeError('retried')):
            with self.assertRaises(RuntimeError):
                send_order_to_holoo.run(order.id)
        self.assertIn('socket down', Order.objects.get(pk=order.pk).holoo_last_error)
        ok = {'success': True, 'InvoiceCode': '36707', 'ErpCode': 'bBAD=', 'SanadCode': '', 'adopted': False}
        with mock.patch('holoo.client.HolooClient.insert_invoice', return_value=ok):
            send_order_to_holoo.run(order.id)
        order.refresh_from_db()
        self.assertEqual((order.holoo_invoice_id, order.holoo_last_error), ('36707', ''))


class AdminVisibilityTests(TaskBase):
    def setUp(self):
        super().setUp()
        cache.delete_many([health.DOWN_SINCE_KEY, health.LAST_ALERT_KEY])
        self.addCleanup(cache.delete_many, [health.DOWN_SINCE_KEY, health.LAST_ALERT_KEY])
        self.client.force_login(CustomUser.objects.create_superuser(phone_number='09120000803', password='x'))

    def test_changelist_warns_while_holoo_is_down(self):
        health._set_time(health.DOWN_SINCE_KEY, timezone.now() - timedelta(minutes=42))
        response = self.client.get(reverse('admin:orders_order_changelist'))
        self.assertContains(response, 'اتصال به وب‌سرویس هلو قطع است')
        self.assertContains(response, 'TNCHolooWebService')
        cache.delete(health.DOWN_SINCE_KEY)
        self.assertNotContains(self.client.get(reverse('admin:orders_order_changelist')), 'اتصال به وب‌سرویس هلو قطع است')

    def test_queued_order_page_shows_the_last_attempt_error(self):
        order = self.make_order(paid=True, method='cash')
        Order.objects.filter(pk=order.pk).update(holoo_last_error='آخرین تلاش ناموفق (10/16 13:14، موقت): Connection refused')
        response = self.client.get(reverse('admin:orders_order_change', args=[order.pk]))
        self.assertContains(response, 'data-testid="holoo-queued"')
        self.assertContains(response, 'data-testid="holoo-attempt-error"')
        self.assertContains(response, 'Connection refused')
