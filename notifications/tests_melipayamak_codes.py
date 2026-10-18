"""
ملی‌پیامک خطای ارسال را با RetStatus=1 و یک کد کوتاه در Value برمی‌گرداند (مثلاً ۱۴ = متن حاوی لینک)؛ ارسال موفق یک شناسه‌ی بلند است.
نسخه‌ی قبلی هر RetStatus=1 را «ارسال‌شده» می‌گرفت و پیام‌های خبرنامه هرگز نمی‌رسیدند (ممیزی ۱۸ اکتبر ۲۰۲۶).
"""
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, override_settings

from notifications.backends.base import NotificationBackendError, NotificationPermanentError
from notifications.backends.melipayamak import ERROR_CODES, MelipayamakBackend
from notifications.models import Notification
from notifications.tasks import deliver_notification


def reply(value, ret_status=1):
    response = mock.Mock(status_code=200)
    response.json.return_value = {'Value': value, 'RetStatus': ret_status, 'StrRetStatus': 'Ok'}
    return response


@override_settings(MELIPAYAMAK_USERNAME='09120000000', MELIPAYAMAK_APIKEY='k', MELIPAYAMAK_FROM_NUMBER='50002710040293')
class ErrorCodeTests(TestCase):
    def send(self, value):
        with mock.patch('notifications.backends.melipayamak.requests.post', return_value=reply(value)):
            return MelipayamakBackend().send('09120000030', 'متن')

    def test_a_long_record_id_is_a_success(self):
        self.assertEqual(self.send('2514249360'), '2514249360')

    def test_content_codes_are_permanent_errors_with_a_persian_reason(self):
        for code, wanted in (('14', 'لینک'), ('15', 'لغو11'), ('7', 'فیلترشده'), ('18', 'نامعتبر')):
            with self.subTest(code=code):
                with self.assertRaises(NotificationPermanentError) as caught:
                    self.send(code)
                self.assertIn(f'کد {code}', str(caught.exception))
                self.assertIn(wanted, str(caught.exception))

    def test_credit_and_rate_codes_stay_retryable(self):
        for code in ('2', '3', '6', '11'):
            with self.subTest(code=code):
                with self.assertRaises(NotificationBackendError) as caught:
                    self.send(code)
                self.assertFalse(caught.exception.permanent)

    def test_an_unknown_short_code_is_a_retryable_error_not_a_success(self):
        with self.assertRaises(NotificationBackendError) as caught:
            self.send('99')
        self.assertFalse(caught.exception.permanent)

    def test_the_shared_template_line_is_checked_the_same_way(self):
        with mock.patch('notifications.backends.melipayamak.requests.post', return_value=reply('15')):
            with self.assertRaises(NotificationPermanentError):
                MelipayamakBackend().send_template('09120000030', 'otp', {'code': '1234'}, 'x')

    def test_table_covers_the_documented_codes(self):
        for code in ('0', '2', '3', '4', '5', '6', '7', '9', '10', '11', '12', '14', '15', '16', '17', '18'):
            self.assertIn(code, ERROR_CODES)


@override_settings(MELIPAYAMAK_USERNAME='09120000000', MELIPAYAMAK_APIKEY='k', MELIPAYAMAK_FROM_NUMBER='50002710040293',
                   NOTIFICATION_BACKEND='notifications.backends.melipayamak.MelipayamakBackend')
class DeliveryTests(TestCase):
    def make(self, text='متن https://example.ir'):
        return Notification.objects.create(recipient='09120000030', template_key='newsletter_campaign', text=text,
                                           backend_override='notifications.backends.melipayamak.MelipayamakBackend')

    def test_a_rejected_message_is_marked_failed_with_the_reason_and_not_retried(self):
        notification = self.make()
        with mock.patch('notifications.backends.melipayamak.requests.post', return_value=reply('14')), \
             mock.patch('notifications.tasks.deliver_notification.retry') as retry:
            outcome = deliver_notification.run(notification.pk)
        self.assertEqual(outcome, 'Rejected.')
        retry.assert_not_called()
        notification.refresh_from_db()
        self.assertEqual(notification.status, Notification.STATUS_FAILED)
        self.assertIn('لینک', notification.error)
        self.assertEqual(notification.attempts, 1)

    def test_a_good_message_is_still_marked_sent(self):
        notification = self.make('متن بدون لینک\nلغو11')
        with mock.patch('notifications.backends.melipayamak.requests.post', return_value=reply('2514249360')):
            deliver_notification.run(notification.pk)
        notification.refresh_from_db()
        self.assertEqual((notification.status, notification.provider_message_id), (Notification.STATUS_SENT, '2514249360'))

    def test_a_retryable_error_still_retries(self):
        notification = self.make()
        with mock.patch('notifications.backends.melipayamak.requests.post', return_value=reply('2')), \
             mock.patch('notifications.tasks.deliver_notification.retry', side_effect=RuntimeError('retried')):
            with self.assertRaises(RuntimeError):
                deliver_notification.run(notification.pk)


class AuditCommandTests(TestCase):
    def make(self, provider_id, status=Notification.STATUS_SENT, backend='notifications.backends.melipayamak.MelipayamakBackend'):
        return Notification.objects.create(recipient='09120000030', template_key='newsletter_welcome', text='x', status=status,
                                           backend=backend, provider_message_id=provider_id)

    def run_command(self, *args):
        out = StringIO()
        call_command('audit_sent_notifications', *args, stdout=out)
        return out.getvalue()

    def test_only_sent_messages_with_a_short_error_code_are_reported(self):
        fake = self.make('14')
        self.make('2514249360')                                         # شناسه‌ی واقعی
        self.make('14', status=Notification.STATUS_FAILED)              # از قبل ناموفق
        self.make('14', backend='notifications.backends.console.ConsoleBackend')
        text = self.run_command()
        self.assertIn('مشکوک (کد خطا به‌جای شناسه): 1', text)
        self.assertIn('newsletter_welcome — کد 14', text)
        self.assertIn('لینک', text)
        fake.refresh_from_db()
        self.assertEqual(fake.status, Notification.STATUS_SENT)         # گزارش فقط می‌خواند

    def test_fix_marks_them_failed_with_the_reason(self):
        fake = self.make('15')
        self.run_command('--fix')
        fake.refresh_from_db()
        self.assertEqual(fake.status, Notification.STATUS_FAILED)
        self.assertIn('لغو11', fake.error)
