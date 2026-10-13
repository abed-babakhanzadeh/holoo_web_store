"""
گیرنده‌ی پیامک‌های سیستمی مدیر (۱۳ اکتبر ۲۰۲۶): «شماره‌ی مدیر ۱ و ۲» تنظیمات سایت برای همه‌ی notify_admin ها معتبر است، نه
فقط پیام «تماس با ما» و چک؛ و فقط وقتی هر دو خالی باشند گیرنده‌ی پیش‌فرض سرور استفاده می‌شود.
"""
from django.core.cache import cache
from django.test import TestCase, override_settings

from notifications.models import Notification
from notifications.service import admin_recipients, notify_admin
from products.models import SiteSettings


class AdminRecipientsTests(TestCase):
    def setUp(self):
        self.addCleanup(cache.delete, SiteSettings.CACHE_KEY)

    def configure(self, first='', second=''):
        SiteSettings.objects.update_or_create(pk=1, defaults={
            'store_admin_sms_recipient': first, 'store_admin_sms_recipient_2': second})
        cache.delete(SiteSettings.CACHE_KEY)

    def recipients_of(self, template_key):
        return sorted(Notification.objects.filter(template_key=template_key).values_list('recipient', flat=True))

    @override_settings(ADMIN_NOTIFICATION_RECIPIENT='09190000000')
    def test_server_default_when_both_numbers_are_empty(self):
        self.configure()
        self.assertEqual(admin_recipients(), ['09190000000'])
        notify_admin('user_registered_admin', phone='09121111111')
        self.assertEqual(self.recipients_of('user_registered_admin'), ['09190000000'])

    @override_settings(ADMIN_NOTIFICATION_RECIPIENT='09190000000')
    def test_saved_numbers_receive_every_system_message_instead_of_the_server_default(self):
        self.configure('09127540293', '09192515466')
        notify_admin('payment_succeeded_admin', order_id=5, amount='1,000', phone='09121111111')
        self.assertEqual(self.recipients_of('payment_succeeded_admin'), ['09127540293', '09192515466'])

    @override_settings(ADMIN_NOTIFICATION_RECIPIENT='09190000000')
    def test_a_single_saved_number_and_deduplication(self):
        self.configure('09127540293', '09127540293')
        notify_admin('user_registered_admin', phone='09121111111')
        self.assertEqual(self.recipients_of('user_registered_admin'), ['09127540293'])
        self.configure('', '09127540293')
        self.assertEqual(admin_recipients(), ['09127540293'])

    @override_settings(ADMIN_NOTIFICATION_RECIPIENT='09190000000')
    def test_return_value_is_the_first_notification(self):
        self.configure('09127540293', '09192515466')
        first = notify_admin('user_registered_admin', phone='09121111111')
        self.assertEqual(first.recipient, '09127540293')

    @override_settings(ADMIN_NOTIFICATION_RECIPIENT='')
    def test_no_recipient_anywhere_is_not_an_error(self):
        self.configure()
        self.assertEqual(admin_recipients(), [])
        self.assertIsNone(notify_admin('user_registered_admin', phone='09121111111'))

    @override_settings(ADMIN_NOTIFICATION_RECIPIENT='09190000000')
    def test_chat_operators_fall_back_to_the_saved_admin_numbers(self):
        from chat.notifier import admin_recipients as chat_recipients

        class Cfg:
            chat_notify_phones = ''
        self.configure('09127540293', '')
        self.assertEqual(chat_recipients(Cfg()), ['09127540293'])
        self.configure()
        self.assertEqual(chat_recipients(Cfg()), ['09190000000'])
