"""
هیچ پیامی نباید ارسال شود در حالی که در تنظیمات پیامک ادمین (روشن/خاموش و متن جای‌گزین) دیده نمی‌شود.
"""
import re
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from notifications.models import Notification, NotificationSetting, sync_notification_settings
from notifications.service import notify
from notifications.templates_registry import TEMPLATES

CALL = re.compile(r"""notify(?:_admin)?\(\s*(?:[^,'"()]+,\s*)?['"]([a-z_]+)['"]""")


def source_files():
    root = Path(settings.BASE_DIR)
    for path in root.rglob('*.py'):
        parts = set(path.parts)
        name = path.name
        if parts & {'migrations', 'node_modules', 'venv', '.venv', '__pycache__'} or name.startswith('tests') or name == 'service.py':
            continue
        yield path


class EverySentMessageIsInTheRegistryTests(TestCase):
    def test_every_literal_key_passed_to_notify_exists_in_the_registry(self):
        used = {}
        for path in source_files():
            for key in CALL.findall(path.read_text(encoding='utf-8')):
                used.setdefault(key, path.name)
        unknown = {key: where for key, where in used.items() if key not in TEMPLATES}
        self.assertEqual(unknown, {}, 'پیامی ارسال می‌شود که در templates_registry (و در نتیجه در تنظیمات پیامک) نیست')
        self.assertGreater(len(used), 10)      # اسکن واقعاً چیزی پیدا کرده

    def test_every_registry_key_has_a_settings_row_after_sync(self):
        NotificationSetting.objects.all().delete()
        created = sync_notification_settings()
        self.assertEqual(created, len(TEMPLATES))
        self.assertEqual(set(NotificationSetting.objects.values_list('template_key', flat=True)), set(TEMPLATES))

    def test_sync_keeps_existing_admin_choices(self):
        sync_notification_settings()
        NotificationSetting.objects.filter(template_key='otp').update(is_enabled=False, custom_body='متن من {code}')
        self.assertEqual(sync_notification_settings(), 0)
        row = NotificationSetting.objects.get(template_key='otp')
        self.assertEqual((row.is_enabled, row.custom_body), (False, 'متن من {code}'))

    def test_new_customer_bulk_messages_start_disabled(self):
        NotificationSetting.objects.all().delete()
        sync_notification_settings()
        self.assertFalse(NotificationSetting.objects.get(template_key='profile_incomplete_reminder_customer').is_enabled)
        self.assertTrue(NotificationSetting.objects.get(template_key='otp').is_enabled)

    def test_the_settings_list_shows_a_template_that_has_no_row_yet(self):
        admin = get_user_model().objects.create_superuser('09120000031', password='x')
        self.client.force_login(admin)
        NotificationSetting.objects.filter(template_key='critical_alert').delete()
        response = self.client.get(reverse('admin:notifications_notificationsetting_changelist'))
        self.assertContains(response, 'critical_alert')
        self.assertTrue(NotificationSetting.objects.filter(template_key='critical_alert').exists())


class NotifyCreatesTheRowTests(TestCase):
    def test_a_message_without_a_row_gets_one_and_still_sends(self):
        NotificationSetting.objects.filter(template_key='otp').delete()
        with mock.patch('notifications.tasks.deliver_notification.delay'), self.captureOnCommitCallbacks(execute=True):
            notification = notify('09120000032', 'otp', code='123456')
        self.assertIsNotNone(notification)
        self.assertTrue(NotificationSetting.objects.get(template_key='otp').is_enabled)

    def test_a_default_disabled_message_without_a_row_is_not_sent(self):
        NotificationSetting.objects.filter(template_key='profile_incomplete_reminder_customer').delete()
        with mock.patch('notifications.tasks.deliver_notification.delay'):
            self.assertIsNone(notify('09120000033', 'profile_incomplete_reminder_customer', name='x'))
        self.assertEqual(Notification.objects.count(), 0)
        self.assertFalse(NotificationSetting.objects.get(template_key='profile_incomplete_reminder_customer').is_enabled)

    def test_the_two_formerly_generic_admin_alerts_have_their_own_switches(self):
        for key in ('order_needs_attention_admin', 'order_rejected_stock_admin'):
            self.assertIn(key, TEMPLATES)
            self.assertTrue(NotificationSetting.objects.filter(template_key=key).exists())
