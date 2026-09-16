"""تست حساب کاربری — محدودیت نرخ OTP، اتمیک بودن مصرف کد، و رجیستری آمار."""

from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomUser, OTPPurpose, OTPRequest, normalize_phone_number
from accounts.throttle import MAX_PER_PHONE_PER_HOUR, RESEND_COOLDOWN


class PhoneNormalizationTests(TestCase):
    def test_accepted_formats(self):
        for raw in ('09123456789', '9123456789', '+989123456789', '989123456789',
                    '00989123456789', '0912 345 6789', '(0912)3456789', '۰۹۱۲۳۴۵۶۷۸۹'):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_phone_number(raw), '09123456789')

    def test_invalid_numbers_are_rejected(self):
        for raw in ('123', '08123456789', '', 'abcdefghijk'):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    normalize_phone_number(raw)


class OTPThrottleTests(TestCase):
    PHONE = '09121234567'

    def setUp(self):
        cache.clear()

    def _send(self, phone=None):
        return self.client.post(reverse('accounts:send_otp'), {'phone_number': phone or self.PHONE})

    def test_first_request_sends_a_code(self):
        self._send()
        self.assertEqual(OTPRequest.objects.filter(phone_number=self.PHONE).count(), 1)

    def test_immediate_resend_is_blocked_by_cooldown(self):
        self._send()
        response = self._send()

        self.assertEqual(OTPRequest.objects.filter(phone_number=self.PHONE).count(), 1)
        self.assertContains(response, str(RESEND_COOLDOWN))

    def test_hourly_cap_per_phone(self):
        for _ in range(MAX_PER_PHONE_PER_HOUR + 3):
            cache.delete(f'otp:cooldown:{self.PHONE}')  # فقط سقف ساعتی را می‌سنجیم
            self._send()

        self.assertEqual(OTPRequest.objects.filter(phone_number=self.PHONE).count(),
                         MAX_PER_PHONE_PER_HOUR)

    def test_cap_is_shared_with_forgot_password_flow(self):
        """ نباید بشود با جابه‌جایی بین دو فرم، سقف را دو برابر کرد """
        CustomUser.objects.create_user(phone_number=self.PHONE)
        for _ in range(MAX_PER_PHONE_PER_HOUR):
            cache.delete(f'otp:cooldown:{self.PHONE}')
            self._send()

        cache.delete(f'otp:cooldown:{self.PHONE}')
        self.client.post(reverse('accounts:forgot_password_send'), {'phone_number': self.PHONE})
        self.assertEqual(OTPRequest.objects.filter(phone_number=self.PHONE).count(),
                         MAX_PER_PHONE_PER_HOUR)

    def test_invalid_phone_does_not_consume_quota(self):
        self.client.post(reverse('accounts:send_otp'), {'phone_number': '123'})
        self.assertEqual(OTPRequest.objects.count(), 0)
        self._send()
        self.assertEqual(OTPRequest.objects.filter(phone_number=self.PHONE).count(), 1)

    def test_successful_login_clears_the_cap(self):
        self._send()
        code = OTPRequest.objects.get(phone_number=self.PHONE).code
        self.client.post(reverse('accounts:verify_otp'), {'phone_number': self.PHONE, 'code': code})

        self._send()  # بدون خطا باید کد تازه بگیرد
        self.assertEqual(OTPRequest.objects.filter(phone_number=self.PHONE).count(), 2)

    def test_code_is_not_generated_with_predictable_random(self):
        """ کد یکبارمصرف باید از secrets بیاید نه random قابل پیش‌بینی """
        import random
        random.seed(0)
        self._send()
        first = OTPRequest.objects.get(phone_number=self.PHONE).code

        OTPRequest.objects.all().delete()
        cache.clear()
        random.seed(0)
        self._send()
        second = OTPRequest.objects.get(phone_number=self.PHONE).code

        self.assertNotEqual(first, second)


class OTPVerifyTests(TestCase):
    PHONE = '09121234568'

    def setUp(self):
        cache.clear()
        self.otp = OTPRequest.objects.create(
            phone_number=self.PHONE, code='123456', purpose=OTPPurpose.REGISTER_LOGIN,
            expires_at=timezone.now() + timedelta(minutes=2),
        )

    def test_correct_code_succeeds(self):
        ok, error, _ = OTPRequest.verify_code(self.PHONE, '123456')
        self.assertTrue(ok)
        self.assertIsNone(error)

    def test_code_cannot_be_used_twice(self):
        """ دو درخواست هم‌زمان با یک کد درست: فقط یکی باید موفق شود """
        self.assertTrue(OTPRequest.verify_code(self.PHONE, '123456')[0])
        # کد مصرف‌شده دیگر پیدا هم نمی‌شود (فیلتر used_at__isnull=True)
        self.assertFalse(OTPRequest.verify_code(self.PHONE, '123456')[0])

    def test_wrong_code_increments_attempt_count(self):
        OTPRequest.verify_code(self.PHONE, '000000')
        OTPRequest.verify_code(self.PHONE, '000000')
        self.otp.refresh_from_db()
        self.assertEqual(self.otp.attempt_count, 2)

    def test_captcha_is_required_after_threshold(self):
        for _ in range(OTPRequest.CAPTCHA_THRESHOLD):
            OTPRequest.verify_code(self.PHONE, '000000')
        self.assertTrue(OTPRequest.captcha_required(self.PHONE))

    def test_expired_code_is_rejected(self):
        OTPRequest.objects.filter(pk=self.otp.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        ok, error, _ = OTPRequest.verify_code(self.PHONE, '123456')
        self.assertFalse(ok)
        self.assertIn('منقضی', error)

    def test_purposes_are_isolated(self):
        ok, _, _ = OTPRequest.verify_code(self.PHONE, '123456', purpose=OTPPurpose.RESET_PASSWORD)
        self.assertFalse(ok)


class DashboardStatsRegistryTests(TestCase):
    def test_every_app_registers_its_own_stats(self):
        from accounts.stats import _providers

        for name, owner in (('orders_total', 'orders.stats'),
                            ('transactions_recent', 'payments.stats'),
                            ('favorites_count', 'wishlist.stats'),
                            ('recently_viewed_count', 'recently_viewed.stats')):
            with self.subTest(name=name):
                self.assertEqual(_providers[name].__module__, owner)

    def test_unknown_stat_returns_default_instead_of_raising(self):
        from accounts.stats import get
        user = CustomUser.objects.create_user(phone_number='09120000050')
        self.assertEqual(get('no_such_stat', user, 'پیش‌فرض'), 'پیش‌فرض')

    def test_failing_provider_does_not_break_the_dashboard(self):
        from accounts import stats
        user = CustomUser.objects.create_user(phone_number='09120000051')
        with mock.patch.dict(stats._providers, {'boom': mock.Mock(side_effect=RuntimeError)}):
            self.assertEqual(stats.get('boom', user, 0), 0)

    def test_paid_orders_count_is_queried_only_once_per_instance(self):
        user = CustomUser.objects.create_user(phone_number='09120000052')
        with self.assertNumQueries(1):
            user.get_loyalty_points()
            user.get_loyalty_level()
            user.get_loyalty_progress_percent()
