"""تست لایه‌ی اطلاع‌رسانی — قابل تعویض بودن موتور و گم‌نشدن پیام."""

from unittest import mock

from django.test import TestCase, override_settings

from notifications.backends.base import NotificationBackend, NotificationBackendError
from notifications.models import Notification
from notifications.service import deliver, get_backend, notify, notify_admin
from notifications.templates_registry import render_message


class CaptureBackend(NotificationBackend):
    """ موتور تستی: فقط ثبت می‌کند که چه چیزی به آن داده شده """
    sent = []

    def send(self, to, text):
        CaptureBackend.sent.append((to, text))
        return 'capture-id'


class BrokenBackend(NotificationBackend):
    def send(self, to, text):
        raise NotificationBackendError('سرویس در دسترس نیست')


CAPTURE = 'notifications.tests.CaptureBackend'
BROKEN = 'notifications.tests.BrokenBackend'


class TemplateTests(TestCase):
    def test_renders_with_context(self):
        self.assertEqual(render_message('otp', {'code': '123456'}),
                         'کد تایید شما برای ورود به فروشگاه: 123456')

    def test_missing_parameter_raises(self):
        with self.assertRaises(KeyError):
            render_message('payment_succeeded_customer', {'name': 'علی'})

    def test_unknown_template_raises(self):
        with self.assertRaises(KeyError):
            render_message('does_not_exist', {})


@override_settings(NOTIFICATION_BACKEND=CAPTURE)
class NotifyTests(TestCase):
    def setUp(self):
        CaptureBackend.sent.clear()

    def test_nothing_is_sent_before_commit(self):
        with mock.patch('notifications.tasks.deliver_notification.delay') as task:
            with self.captureOnCommitCallbacks(execute=False):
                notify('09120000030', 'otp', code='111111')
            self.assertEqual(task.call_count, 0)

    def test_task_is_fired_after_commit(self):
        with mock.patch('notifications.tasks.deliver_notification.delay') as task:
            with self.captureOnCommitCallbacks(execute=True):
                notify('09120000030', 'otp', code='111111')
        self.assertEqual(task.call_count, 1)

    def test_backend_only_receives_recipient_and_text(self):
        """ قرارداد موتور فقط (مقصد، متن) است — نه سفارش، نه کاربر، نه نوع پیام """
        with mock.patch('notifications.tasks.deliver_notification.delay'):
            notification = notify('09120000030', 'otp', code='111111')
        deliver(notification)

        self.assertEqual(CaptureBackend.sent,
                         [('09120000030', 'کد تایید شما برای ورود به فروشگاه: 111111')])

    def test_successful_delivery_is_recorded(self):
        with mock.patch('notifications.tasks.deliver_notification.delay'):
            notification = notify('09120000030', 'otp', code='111111')
        deliver(notification)
        notification.refresh_from_db()

        self.assertEqual(notification.status, Notification.STATUS_SENT)
        self.assertEqual(notification.attempts, 1)
        self.assertEqual(notification.provider_message_id, 'capture-id')
        self.assertIsNotNone(notification.sent_at)

    def test_missing_parameter_does_not_create_notification(self):
        with mock.patch('notifications.tasks.deliver_notification.delay'):
            result = notify('09120000030', 'payment_succeeded_customer', name='علی')
        self.assertIsNone(result)
        self.assertEqual(Notification.objects.count(), 0)

    def test_empty_recipient_is_skipped(self):
        self.assertIsNone(notify('', 'otp', code='111111'))
        self.assertEqual(Notification.objects.count(), 0)

    @override_settings(ADMIN_NOTIFICATION_RECIPIENT='09199999999')
    def test_notify_admin_uses_configured_recipient(self):
        with mock.patch('notifications.tasks.deliver_notification.delay'):
            notification = notify_admin('critical_alert', message='تست')
        self.assertEqual(notification.recipient, '09199999999')


class BackendSwapTests(TestCase):
    def test_backend_is_selected_from_settings(self):
        with override_settings(NOTIFICATION_BACKEND=CAPTURE):
            self.assertIsInstance(get_backend(), CaptureBackend)
        with override_settings(NOTIFICATION_BACKEND='notifications.backends.email.EmailBackend'):
            from notifications.backends.email import EmailBackend
            self.assertIsInstance(get_backend(), EmailBackend)

    def test_default_backend_is_console(self):
        from notifications.backends.console import ConsoleBackend
        self.assertIsInstance(get_backend(), ConsoleBackend)


@override_settings(NOTIFICATION_BACKEND=BROKEN)
class FailureTests(TestCase):
    def test_failure_is_recorded_and_raised(self):
        """ شکست ارسال نباید بی‌سروصدا بلعیده شود؛ باید قابل پیگیری و تلاش مجدد بماند """
        with mock.patch('notifications.tasks.deliver_notification.delay'):
            notification = notify('09120000031', 'otp', code='222222')

        with self.assertRaises(NotificationBackendError):
            deliver(notification)

        notification.refresh_from_db()
        self.assertEqual(notification.status, Notification.STATUS_FAILED)
        self.assertEqual(notification.attempts, 1)
        self.assertIn('در دسترس نیست', notification.error)

    def test_broker_failure_leaves_notification_pending_for_retry(self):
        with mock.patch('notifications.tasks.deliver_notification.delay', side_effect=OSError('redis down')):
            with self.captureOnCommitCallbacks(execute=True):
                notification = notify('09120000032', 'otp', code='333333')

        notification.refresh_from_db()
        self.assertEqual(notification.status, Notification.STATUS_PENDING)
