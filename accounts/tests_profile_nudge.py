"""
ترغیب مشتری به تکمیل پروفایل: نوار بالای سایت، پیام‌های درست (نه «منتظر مدیر» برای پروفایل ناقص)، هدایت بعد از ورود، و
پیامک یادآوری (قابل روشن/خاموش از تنظیمات پیامک ادمین).
"""
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomUser
from accounts.tasks import REMINDER_KEY, eligible_for_reminder, send_profile_reminders
from notifications.models import Notification, NotificationSetting
from products.templatetags.product_tags import price_hidden_reason

TEHRAN = ZoneInfo('Asia/Tehran')


def make_user(phone='09121110001', **kwargs):
    return CustomUser.objects.create_user(phone, **kwargs)


class NeedsProfileCompletionTests(TestCase):
    def test_incomplete_customer_needs_completion(self):
        self.assertTrue(make_user().needs_profile_completion)
        self.assertTrue(make_user('09121110002', first_name='الف', last_name='ب').needs_profile_completion)

    def test_complete_customer_does_not(self):
        self.assertFalse(make_user(first_name='الف', last_name='ب', national_code='0012345678').needs_profile_completion)

    def test_staff_never_does(self):
        self.assertFalse(make_user(is_staff=True).needs_profile_completion)

    def test_price_hidden_reason_distinguishes_incomplete_from_pending(self):
        self.assertEqual(price_hidden_reason(make_user()), 'incomplete')
        complete = make_user('09121110003', first_name='الف', last_name='ب', national_code='0012345678')
        self.assertEqual(price_hidden_reason(complete), 'pending')


class BannerAndMessagesTests(TestCase):
    def setUp(self):
        self.user = make_user('09121110010')
        self.client.force_login(self.user)

    def test_banner_is_shown_site_wide_to_an_incomplete_profile(self):
        response = self.client.get(reverse('products:product_list'))
        self.assertContains(response, 'تکمیل پروفایل')
        self.assertContains(response, reverse('accounts:profile_complete'))

    def test_banner_is_personal_for_a_former_holoo_customer(self):
        CustomUser.objects.filter(pk=self.user.pk).update(imported_from_holoo=True)
        self.assertContains(self.client.get(reverse('products:product_list')), 'شما مشتری قبلی ما هستید')

    def test_banner_is_not_repeated_on_the_completion_page_itself(self):
        self.assertNotContains(self.client.get(reverse('accounts:profile_complete')), 'مشتری قبلی ما هستید')
        self.assertNotContains(self.client.get(reverse('accounts:profile_complete')), 'bg-yellow-50')

    def test_no_banner_for_a_complete_profile_or_staff(self):
        complete = make_user('09121110011', first_name='الف', last_name='ب', national_code='0012345678')
        self.client.force_login(complete)
        self.assertNotContains(self.client.get(reverse('products:product_list')), 'برای مشاهده‌ی قیمت‌ها و ثبت سفارش، لطفاً')

    def test_dashboard_asks_for_the_profile_instead_of_claiming_to_wait_for_the_admin(self):
        response = self.client.get(reverse('accounts:dashboard'))
        self.assertContains(response, 'تکمیل پروفایل')
        self.assertNotContains(response, 'حساب شما در انتظار تأیید مدیریت است')

    def test_a_complete_but_unapproved_profile_still_waits_for_the_admin(self):
        complete = make_user('09121110012', first_name='الف', last_name='ب', national_code='0012345678')
        self.client.force_login(complete)
        self.assertContains(self.client.get(reverse('accounts:dashboard')), 'حساب شما در انتظار تأیید مدیریت است')


class LoginRedirectTests(TestCase):
    def verify(self, user, next_url=''):
        with mock.patch('accounts.views.OTPRequest.verify_code', return_value=(True, '', 0)), \
                mock.patch('accounts.views.OTPRequest.captcha_required', return_value=False):
            return self.client.post(reverse('accounts:verify_otp'), {'phone_number': user.phone_number, 'code': '123456', 'next': next_url})

    def test_an_incomplete_profile_is_sent_to_the_completion_page_and_keeps_the_destination(self):
        user = make_user('09121110020')
        response = self.verify(user, '/products/')
        self.assertEqual(response['HX-Redirect'], reverse('accounts:profile_complete'))
        self.assertEqual(self.client.session['profile_complete_next'], '/products/')

    def test_after_completing_the_original_destination_is_restored(self):
        user = make_user('09121110021')
        self.verify(user, '/products/')
        with mock.patch('holoo.receivers.sync_user_to_holoo.delay'):
            response = self.client.post(reverse('accounts:profile_complete'), {
                'first_name': 'الف', 'last_name': 'ب', 'national_code': '1234567890',
                'password': 'StrongPass123!', 'confirm_password': 'StrongPass123!'})
        self.assertEqual(response['HX-Redirect'], '/products/')

    def test_a_complete_profile_goes_straight_to_the_destination(self):
        user = make_user('09121110022', first_name='الف', last_name='ب', national_code='0012345678')
        self.assertEqual(self.verify(user, '/products/')['HX-Redirect'], '/products/')

    def test_staff_are_not_forced_to_complete_a_customer_profile(self):
        user = make_user('09121110023', is_staff=True)
        self.assertEqual(self.verify(user, '/admin/')['HX-Redirect'], '/admin/')

    def test_an_unsafe_next_is_still_neutralized(self):
        user = make_user('09121110024', first_name='الف', last_name='ب', national_code='0012345678')
        self.assertEqual(self.verify(user, 'https://evil.example/')['HX-Redirect'], '/')


class EligibilityTests(SimpleTestCase):
    NOW = datetime(2026, 10, 6, 12, 0, tzinfo=TEHRAN)

    def user(self, **kw):
        user = CustomUser(phone_number='09120000000', is_active=True, **kw)
        user.last_login = kw.get('last_login', self.NOW - timedelta(hours=30))
        return user

    def test_first_reminder_only_after_24_hours_since_last_login(self):
        self.assertTrue(eligible_for_reminder(self.user(), 0, None, self.NOW))
        recent = self.user(last_login=self.NOW - timedelta(hours=5))
        self.assertFalse(eligible_for_reminder(recent, 0, None, self.NOW))

    def test_a_dormant_user_is_not_chased(self):
        old = self.user(last_login=self.NOW - timedelta(days=30))
        self.assertFalse(eligible_for_reminder(old, 0, None, self.NOW))

    def test_second_reminder_after_three_days_then_stop(self):
        self.assertFalse(eligible_for_reminder(self.user(), 1, self.NOW - timedelta(days=2), self.NOW))
        self.assertTrue(eligible_for_reminder(self.user(), 1, self.NOW - timedelta(days=3, hours=1), self.NOW))
        self.assertFalse(eligible_for_reminder(self.user(), 2, self.NOW - timedelta(days=30), self.NOW))

    def test_never_logged_in_or_complete_profiles_are_skipped(self):
        never = CustomUser(phone_number='09120000001', is_active=True)
        never.last_login = None
        self.assertFalse(eligible_for_reminder(never, 0, None, self.NOW))
        complete = self.user(first_name='الف', last_name='ب', national_code='0012345678')
        self.assertFalse(eligible_for_reminder(complete, 0, None, self.NOW))


class ReminderTaskTests(TestCase):
    NOW = datetime(2026, 10, 6, 12, 0, tzinfo=TEHRAN)

    def setUp(self):
        self.user = make_user('09121110030', first_name='مهدی')
        CustomUser.objects.filter(pk=self.user.pk).update(last_login=self.NOW - timedelta(hours=30))
        self.send = mock.patch('notifications.tasks.deliver_notification.delay').start()
        self.addCleanup(mock.patch.stopall)

    def enable(self, on=True):
        from notifications.models import sync_notification_settings
        sync_notification_settings()
        NotificationSetting.objects.filter(template_key=REMINDER_KEY).update(is_enabled=on)

    def reminders(self):
        return Notification.objects.filter(template_key=REMINDER_KEY).count()

    def test_it_is_off_by_default_so_nothing_is_sent_until_the_admin_turns_it_on(self):
        self.assertEqual(send_profile_reminders(now=self.NOW), 'disabled')
        self.assertEqual(self.reminders(), 0)
        self.assertFalse(NotificationSetting.objects.get(template_key=REMINDER_KEY).is_enabled)

    def test_when_enabled_a_reminder_is_sent_once(self):
        self.enable()
        self.assertEqual(send_profile_reminders(now=self.NOW), 'sent=1')
        self.assertEqual(send_profile_reminders(now=self.NOW + timedelta(minutes=5)), 'sent=0')
        self.assertEqual(self.reminders(), 1)
        self.assertIn('مهدی گرامی', Notification.objects.get(template_key=REMINDER_KEY).text)

    def test_second_after_three_days_and_never_a_third(self):
        self.enable()
        send_profile_reminders(now=self.NOW)
        Notification.objects.filter(template_key=REMINDER_KEY).update(created_at=self.NOW - timedelta(days=3, hours=1))
        CustomUser.objects.filter(pk=self.user.pk).update(last_login=self.NOW - timedelta(days=4))
        self.assertEqual(send_profile_reminders(now=self.NOW), 'sent=1')
        Notification.objects.filter(template_key=REMINDER_KEY).update(created_at=self.NOW - timedelta(days=10))
        self.assertEqual(send_profile_reminders(now=self.NOW + timedelta(days=1)), 'sent=0')
        self.assertEqual(self.reminders(), 2)

    def test_nothing_is_sent_at_night(self):
        self.enable()
        night = datetime(2026, 10, 6, 23, 30, tzinfo=TEHRAN)
        self.assertEqual(send_profile_reminders(now=night), 'outside sending hours')
        self.assertEqual(send_profile_reminders(now=datetime(2026, 10, 6, 6, 0, tzinfo=TEHRAN)), 'outside sending hours')

    def test_a_user_who_completed_the_profile_is_not_reminded(self):
        self.enable()
        CustomUser.objects.filter(pk=self.user.pk).update(last_name='ب', national_code='0012345678')
        self.assertEqual(send_profile_reminders(now=self.NOW), 'sent=0')

    def test_turning_the_setting_off_stops_it_immediately(self):
        self.enable()
        self.enable(on=False)
        self.assertEqual(send_profile_reminders(now=self.NOW), 'disabled')
