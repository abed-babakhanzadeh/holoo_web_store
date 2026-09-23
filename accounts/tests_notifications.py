"""
فاز ۴: اعلان‌ها/سیگنال‌های چرخه‌ی تأیید تجاری — عدم ارسال تکراری (dedup) و صحت زمان‌بندی
(فقط پس از commit، فقط وقتی واقعاً changed=True بود).

الگوی mock دقیقاً هم‌شکل orders.tests.SubmitOrderTests.test_order_placed_notifies_customer:
mock.patch('notifications.receivers.notify'/'notify_admin') — چون receivers.py این دو تابع را
با نام مستقیم import کرده، پچ‌کردن آن‌جا دقیقاً همان چیزی را می‌گیرد که گیرنده‌ی سیگنال صدا می‌زند.

سیگنال‌هایی که از متدهای مدل (approve/reject/resubmit_for_review) با transaction.on_commit شلیک
می‌شوند، نیاز به self.captureOnCommitCallbacks(execute=True) دارند تا در TestCase معمولی
(تراکنش بیرونی رول‌بک‌شونده) واقعاً اجرا شوند؛ user_registered (از VerifyOTPView، بدون atomic
دورش) نیاز ندارد.
"""

import threading
from unittest import mock

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse

from accounts.models import ApprovalStatus, CustomUser, OTPRequest, normalize_phone_number
from notifications.models import Notification


class UserRegisteredNotificationTests(TestCase):
    """ اعلان مدیر هنگام ثبت‌نام اولیه (VerifyOTPView، created=True) — نه هنگام ورود دوباره """

    def setUp(self):
        cache.clear()

    def _send_and_verify(self, phone):
        self.client.post(reverse('accounts:send_otp'), {'phone_number': phone})
        otp = OTPRequest.objects.filter(phone_number=normalize_phone_number(phone)).latest('created_at')
        return self.client.post(reverse('accounts:verify_otp'), {'phone_number': phone, 'code': otp.code})

    def test_new_phone_number_notifies_admin_of_registration(self):
        with mock.patch('notifications.receivers.notify_admin') as notify_admin_mock:
            response = self._send_and_verify('09121110001')
        self.assertEqual(response.status_code, 200)
        notify_admin_mock.assert_called_once_with('user_registered_admin', phone='09121110001')

    def test_existing_user_logging_in_again_does_not_refire_registration_notice(self):
        self._send_and_verify('09121110002')  # ثبت‌نام واقعی، خارج از mock
        with mock.patch('notifications.receivers.notify_admin') as notify_admin_mock:
            self._send_and_verify('09121110002')  # همان شماره، ورود دوباره با OTP تازه
        notify_admin_mock.assert_not_called()


class ProfileCompletedNotificationDedupTests(TestCase):
    """ اعلان تکمیل پروفایل فقط بار اول؛ رفرش/ساب‌میت دوباره‌ی فرم پیامک تکراری نمی‌زند """

    def setUp(self):
        self.user = CustomUser.objects.create_user(phone_number='09121110010')
        self.client.force_login(self.user)

    def _complete(self):
        return self.client.post(reverse('accounts:profile_complete'), {
            'first_name': 'الف', 'last_name': 'ب', 'national_code': '1234567890',
            'password': 'StrongPass123!', 'confirm_password': 'StrongPass123!',
        })

    def test_first_completion_notifies_admin(self):
        with mock.patch('notifications.receivers.notify_admin') as notify_admin_mock:
            self._complete()
        notify_admin_mock.assert_called_once_with('profile_completed_admin', full_name='الف ب', phone='09121110010')

    def test_resubmitting_the_same_form_does_not_notify_again(self):
        self._complete()
        with mock.patch('notifications.receivers.notify_admin') as notify_admin_mock:
            self._complete()  # رفرش/دابل‌ساب‌میت همان فرم
        notify_admin_mock.assert_not_called()

    def test_invalid_form_does_not_notify(self):
        with mock.patch('notifications.receivers.notify_admin') as notify_admin_mock:
            self.client.post(reverse('accounts:profile_complete'), {
                'first_name': '', 'last_name': '', 'national_code': 'bad',
                'password': 'x', 'confirm_password': 'y',
            })
        notify_admin_mock.assert_not_called()


class ResubmitForReviewTests(TestCase):
    """ دکمه‌ی «ارسال مجدد جهت بررسی»: فقط از REJECTED معنا دارد و فقط آن‌جا اعلان می‌رود """

    def setUp(self):
        self.user = CustomUser.objects.create_user(
            phone_number='09121110020', first_name='ج', last_name='د', national_code='1112223334',
        )

    def test_rejected_user_can_resubmit_and_admin_is_notified(self):
        self.user.reject(reason='ناقص')
        self.client.force_login(self.user)
        with mock.patch('notifications.receivers.notify_admin') as notify_admin_mock:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(reverse('accounts:resubmit_for_review'))
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.approval_status, ApprovalStatus.PENDING)
        notify_admin_mock.assert_called_once_with('user_resubmitted_admin', full_name='ج د', phone='09121110020')

    def test_pending_user_cannot_trigger_a_resubmit_notification(self):
        self.client.force_login(self.user)  # پیش‌فرض PENDING است، نه REJECTED
        with mock.patch('notifications.receivers.notify_admin') as notify_admin_mock:
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(reverse('accounts:resubmit_for_review'))
        notify_admin_mock.assert_not_called()

    def test_approved_user_cannot_trigger_a_resubmit_notification(self):
        self.user.approve(price_level=1)
        self.client.force_login(self.user)
        with mock.patch('notifications.receivers.notify_admin') as notify_admin_mock:
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(reverse('accounts:resubmit_for_review'))
        notify_admin_mock.assert_not_called()

    def test_sequential_double_submit_notifies_only_once(self):
        self.user.reject(reason='ناقص')
        self.client.force_login(self.user)
        with mock.patch('notifications.receivers.notify_admin') as notify_admin_mock:
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(reverse('accounts:resubmit_for_review'))
                self.client.post(reverse('accounts:resubmit_for_review'))  # کلیک دوباره روی همان دکمه
        self.assertEqual(notify_admin_mock.call_count, 1)


class ResubmitForReviewConcurrencyTests(TransactionTestCase):
    """
    دو درخواست *واقعاً* هم‌زمان (نه ترتیبی) به /resubmit-for-review/ روی یک کاربر REJECTED —
    دقیقاً همان روش accounts.tests_approval_concurrency (threading.Barrier، اتصال دیتابیس مجزا
    به ازای هر Thread). چون TransactionTestCase واقعاً commit می‌کند، به‌جای mock مستقیم تعداد
    ردیف‌های واقعی Notification را می‌شماریم.
    """
    serialized_rollback = True

    def test_two_concurrent_resubmits_notify_admin_exactly_once(self):
        user = CustomUser.objects.create_user(
            phone_number='09121110021', first_name='ه', last_name='و', national_code='1112223335',
        )
        user.reject(reason='تست هم‌زمانی')

        barrier = threading.Barrier(2)
        results = [None, None]

        def worker(index):
            try:
                barrier.wait(timeout=30)
                client = Client()
                client.force_login(user)
                results[index] = client.post(reverse('accounts:resubmit_for_review'))
            except BaseException as error:                                # noqa: BLE001
                results[index] = error
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertFalse(any(t.is_alive() for t in threads), 'یک نخ گیر کرد (احتمال deadlock)')
        for r in results:
            self.assertNotIsInstance(r, BaseException, msg=f'یک نخ استثنا داد: {r!r}')

        user.refresh_from_db()
        self.assertEqual(user.approval_status, ApprovalStatus.PENDING)
        # notify_admin به ADMIN_NOTIFICATION_RECIPIENT می‌فرستد (نه به خودِ کاربر)؛ شماره‌ی کاربر فقط
        # داخل متن/context پیام است، پس اینجا صرفاً روی template_key فیلتر می‌کنیم
        self.assertEqual(Notification.objects.filter(template_key='user_resubmitted_admin').count(), 1)


class UserApprovedNotificationTests(TestCase):
    """ اعلان تأیید حساب به مشتری: فقط پس از commit، فقط یک‌بار """

    def setUp(self):
        self.user = CustomUser.objects.create_user(
            phone_number='09121110030', first_name='ز', last_name='ح', national_code='1112223336',
        )

    def test_approve_does_not_notify_before_commit(self):
        """ بدون captureOnCommitCallbacks، تراکنشِ بیرونیِ TestCase هرگز واقعاً commit نمی‌شود؛
        پس callback ثبت‌شده‌ی داخل approve() هم هرگز اجرا نمی‌شود """
        with mock.patch('notifications.receivers.notify') as notify_mock:
            self.user.approve(price_level=1)
            notify_mock.assert_not_called()

    def test_approve_notifies_customer_exactly_once_after_commit(self):
        with mock.patch('notifications.receivers.notify') as notify_mock:
            with self.captureOnCommitCallbacks(execute=True):
                self.user.approve(price_level=1)
        notify_mock.assert_called_once_with('09121110030', 'account_approved_customer', name='ز')

    def test_approving_an_already_approved_user_does_not_notify_again(self):
        self.user.approve(price_level=1)  # اولین‌بار، خارج از mock
        with mock.patch('notifications.receivers.notify') as notify_mock:
            with self.captureOnCommitCallbacks(execute=True):
                self.user.approve(price_level=5)  # حتی با سطح دیگر هم بی‌اثر است (changed=False)
        notify_mock.assert_not_called()


class NoNotificationOnRollbackTests(TestCase):
    """ اگر تراکنش رول‌بک شود (خطای اعتبارسنجی)، هیچ اعلانی نباید صادر/ثبت شود """

    def test_approve_with_invalid_price_level_does_not_notify_and_stays_pending(self):
        user = CustomUser.objects.create_user(
            phone_number='09121110050', first_name='ک', last_name='ل', national_code='1112223338',
        )
        with mock.patch('notifications.receivers.notify') as notify_mock:
            with self.captureOnCommitCallbacks(execute=True):
                with self.assertRaises(ValidationError):
                    user.approve(price_level=99)
        notify_mock.assert_not_called()
        user.refresh_from_db()
        self.assertEqual(user.approval_status, ApprovalStatus.PENDING)
        self.assertFalse(Notification.objects.filter(recipient=user.phone_number).exists())

    def test_approve_of_incomplete_profile_does_not_notify(self):
        user = CustomUser.objects.create_user(phone_number='09121110051')  # بدون نام/کدملی
        with mock.patch('notifications.receivers.notify') as notify_mock:
            with self.captureOnCommitCallbacks(execute=True):
                with self.assertRaises(ValidationError):
                    user.approve(price_level=1)
        notify_mock.assert_not_called()


class IdentityChangeRevocationTests(TestCase):
    """ تغییر فیلد هویتی (نام/نام‌خانوادگی/کدملی) تأیید را باطل می‌کند؛ فیلد غیرهویتی (ایمیل) نه """

    def setUp(self):
        self.user = CustomUser.objects.create_user(
            phone_number='09121110040', first_name='ط', last_name='ی', national_code='1112223337',
        )
        self.user.approve(price_level=3)
        self.client.force_login(self.user)

    def _post_profile(self, **overrides):
        data = {
            'first_name': self.user.first_name, 'last_name': self.user.last_name,
            'national_code': self.user.national_code, 'email': '',
        }
        data.update(overrides)
        return self.client.post(reverse('accounts:profile'), data)

    def test_changing_first_name_revokes_approval(self):
        self._post_profile(first_name='نام‌تازه')
        self.user.refresh_from_db()
        self.assertEqual(self.user.approval_status, ApprovalStatus.PENDING)
        self.assertEqual(self.user.price_level, 3)  # سطح قیمت قبلی دست‌نخورده می‌ماند

    def test_changing_national_code_revokes_approval(self):
        self._post_profile(national_code='9998887776')
        self.user.refresh_from_db()
        self.assertEqual(self.user.approval_status, ApprovalStatus.PENDING)

    def test_changing_only_email_does_not_revoke_approval(self):
        self._post_profile(email='veteran@example.com')
        self.user.refresh_from_db()
        self.assertEqual(self.user.approval_status, ApprovalStatus.APPROVED)

    def test_resaving_identical_data_does_not_revoke_approval(self):
        self._post_profile()  # همان مقادیر فعلی، بدون تغییر واقعی
        self.user.refresh_from_db()
        self.assertEqual(self.user.approval_status, ApprovalStatus.APPROVED)

    def test_pending_or_rejected_user_editing_identity_is_not_affected(self):
        """ revoke فقط برای APPROVED معنا دارد؛ کاربر PENDING/REJECTED از قبل هم قیمتی نمی‌بیند """
        other = CustomUser.objects.create_user(
            phone_number='09121110041', first_name='م', last_name='ن', national_code='1112223339',
        )
        self.client.force_login(other)
        response = self.client.post(reverse('accounts:profile'), {
            'first_name': 'تغییریافته', 'last_name': 'ن', 'national_code': '1112223339', 'email': '',
        })
        self.assertEqual(response.status_code, 200)
        other.refresh_from_db()
        self.assertEqual(other.approval_status, ApprovalStatus.PENDING)  # همان‌طور مانده، نه یک استثنا
