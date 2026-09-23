"""
تست‌های چرخه‌ی تأیید تجاری مشتری (فاز ۱: مدل + CheckConstraint + متدها).

پوشش: متدهای approve/reject/resubmit_for_review/revoke_approval_due_to_identity_change
(اتمیک، idempotent، سیگنال فقط پس از commit)، can_view_prices/can_order (تفکیک staff از
approval_status)، clean() برای UX ادمین، و CheckConstraint سطح دیتابیس به‌عنوان تضمین واقعی
و بدون‌استثنا (طبق همان الگوی SiteSettingsGuestPricingTests در products/tests.py).

تست‌های هم‌زمانی واقعی (threading.Barrier روی SQL Server) در فایل جداگانه‌ی
accounts/tests_approval_concurrency.py هستند (سنگین‌تر و کندترند).
"""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from accounts.models import ApprovalStatus, CustomUser, UserStatus
from accounts.signals import (
    user_approved, user_identity_changed_after_approval, user_rejected, user_resubmitted_for_review,
)


def _complete_profile_user(**extra):
    """ کاربری با پروفایل کامل (اسم/فامیل/کدملی) و status از قبل PENDING_ERP_SYNC (مثل کاربر واقعیِ
    تازه پروفایل‌تکمیل‌کرده)، پیش‌فرض approval_status=PENDING """
    fields = dict(
        first_name='علی', last_name='رضایی', national_code='1234567890',
        status=UserStatus.PENDING_ERP_SYNC,
    )
    fields.update(extra)
    return CustomUser.objects.create_user(phone_number=fields.pop('phone_number', '09120001001'), **fields)


class ApproveMethodTests(TestCase):
    def setUp(self):
        self.user = _complete_profile_user()

    def test_approve_sets_status_price_level_and_fires_signal_after_commit(self):
        received = []
        user_approved.connect(lambda sender, user, **kw: received.append(user.pk), weak=False)

        with self.captureOnCommitCallbacks(execute=True):
            locked, changed = self.user.approve(price_level=3, approved_by=None)

        self.assertTrue(changed)
        self.assertEqual(locked.approval_status, ApprovalStatus.APPROVED)
        self.assertEqual(locked.price_level, 3)
        self.assertIsNotNone(locked.approved_at)
        self.assertEqual(received, [self.user.pk])

    def test_approve_is_idempotent_on_already_approved_user(self):
        self.user.approve(price_level=2)
        received = []
        user_approved.connect(lambda sender, user, **kw: received.append(user.pk), weak=False)

        with self.captureOnCommitCallbacks(execute=True):
            locked, changed = self.user.approve(price_level=7)  # حتی سطح متفاوت هم بی‌اثر است

        self.assertFalse(changed)
        self.assertEqual(locked.price_level, 2)  # همان سطح قبلی، تغییر نکرد
        self.assertEqual(received, [])           # بدون پیامک/سیگنال تکراری

    def test_approve_raises_for_incomplete_profile_and_does_not_change_state(self):
        incomplete = CustomUser.objects.create_user(phone_number='09120001002')  # بدون نام/کدملی
        with self.assertRaises(ValidationError):
            incomplete.approve(price_level=1)
        incomplete.refresh_from_db()
        self.assertEqual(incomplete.approval_status, ApprovalStatus.PENDING)  # رول‌بک کامل

    def test_approve_raises_for_invalid_price_level(self):
        with self.assertRaises(ValidationError):
            self.user.approve(price_level=11)
        with self.assertRaises(ValidationError):
            self.user.approve(price_level=0)
        self.user.refresh_from_db()
        self.assertEqual(self.user.approval_status, ApprovalStatus.PENDING)

    def test_valid_price_levels_is_dynamic_not_hardcoded(self):
        self.assertEqual(CustomUser.valid_price_levels(), dict(CustomUser.PRICE_LEVELS))
        self.assertEqual(set(CustomUser.valid_price_levels()), set(range(1, 11)))


class RejectMethodTests(TestCase):
    def setUp(self):
        self.user = _complete_profile_user(phone_number='09120001003')

    def test_reject_sets_status_reason_and_fires_signal(self):
        received = []
        user_rejected.connect(lambda sender, user, **kw: received.append(user.pk), weak=False)

        with self.captureOnCommitCallbacks(execute=True):
            locked, changed = self.user.reject(reason='مدرک هویتی ناقص است')

        self.assertTrue(changed)
        self.assertEqual(locked.approval_status, ApprovalStatus.REJECTED)
        self.assertEqual(locked.rejection_reason, 'مدرک هویتی ناقص است')
        self.assertEqual(received, [self.user.pk])

    def test_reject_is_idempotent_on_already_rejected_user(self):
        self.user.reject(reason='اول')
        received = []
        user_rejected.connect(lambda sender, user, **kw: received.append(user.pk), weak=False)

        with self.captureOnCommitCallbacks(execute=True):
            locked, changed = self.user.reject(reason='دوم')

        self.assertFalse(changed)
        self.assertEqual(locked.rejection_reason, 'اول')  # دلیل قبلی دست‌نخورده
        self.assertEqual(received, [])


class ResubmitForReviewMethodTests(TestCase):
    def setUp(self):
        self.user = _complete_profile_user(phone_number='09120001004')
        self.user.reject(reason='چیزی')

    def test_resubmit_transitions_rejected_to_pending_and_fires_signal(self):
        received = []
        user_resubmitted_for_review.connect(lambda sender, user, **kw: received.append(user.pk), weak=False)

        with self.captureOnCommitCallbacks(execute=True):
            locked, changed = self.user.resubmit_for_review()

        self.assertTrue(changed)
        self.assertEqual(locked.approval_status, ApprovalStatus.PENDING)
        self.assertEqual(received, [self.user.pk])

    def test_resubmit_is_noop_when_not_rejected(self):
        self.user.resubmit_for_review()  # الان PENDING است
        received = []
        user_resubmitted_for_review.connect(lambda sender, user, **kw: received.append(user.pk), weak=False)

        with self.captureOnCommitCallbacks(execute=True):
            locked, changed = self.user.resubmit_for_review()  # دابل‌کلیک روی دکمه

        self.assertFalse(changed)
        self.assertEqual(received, [])


class RevokeApprovalDueToIdentityChangeTests(TestCase):
    def setUp(self):
        self.user = _complete_profile_user(phone_number='09120001005')
        self.user.approve(price_level=4)

    def test_revoke_transitions_approved_to_pending_and_fires_signal(self):
        received = []
        user_identity_changed_after_approval.connect(lambda sender, user, **kw: received.append(user.pk), weak=False)

        with self.captureOnCommitCallbacks(execute=True):
            locked, changed = self.user.revoke_approval_due_to_identity_change()

        self.assertTrue(changed)
        self.assertEqual(locked.approval_status, ApprovalStatus.PENDING)
        self.assertEqual(locked.price_level, 4)  # سطح قبلی دست‌نخورده می‌ماند، فقط دسترسی بسته می‌شود
        self.assertEqual(received, [self.user.pk])

    def test_revoke_is_noop_when_not_approved(self):
        pending_user = _complete_profile_user(phone_number='09120001006')
        received = []
        user_identity_changed_after_approval.connect(lambda sender, user, **kw: received.append(user.pk), weak=False)

        with self.captureOnCommitCallbacks(execute=True):
            locked, changed = pending_user.revoke_approval_due_to_identity_change()

        self.assertFalse(changed)
        self.assertEqual(received, [])


class CanViewPricesVsCanOrderTests(TestCase):
    """ تفکیک صریح دو مجوز — نکته‌ی کلیدی: staff/superuser فقط can_view_prices را معاف می‌کند """

    def test_pending_non_staff_cannot_view_or_order(self):
        user = _complete_profile_user(phone_number='09120001007')
        self.assertFalse(user.can_view_prices())
        self.assertFalse(user.can_order())

    def test_approved_non_staff_can_view_and_order(self):
        user = _complete_profile_user(phone_number='09120001008')
        user, _ = user.approve(price_level=1)  # approve() نمونه‌ی جدید می‌دهد؛ user قدیمی در حافظه به‌روز نمی‌شود
        self.assertTrue(user.can_view_prices())
        self.assertTrue(user.can_order())

    def test_rejected_non_staff_cannot_view_or_order(self):
        user = _complete_profile_user(phone_number='09120001009')
        user.reject()
        self.assertFalse(user.can_view_prices())
        self.assertFalse(user.can_order())

    def test_staff_can_view_prices_but_cannot_order_without_approval(self):
        staff = CustomUser.objects.create_user(phone_number='09120001010', is_staff=True)
        self.assertTrue(staff.can_view_prices())   # معاف برای مدیریت کاتالوگ
        self.assertFalse(staff.can_order())         # ولی برای خرید واقعی باید مثل هر مشتری تأیید شود

    def test_superuser_can_view_prices_but_cannot_order_without_approval(self):
        su = CustomUser.objects.create_user(phone_number='09120001011', is_superuser=True)
        self.assertTrue(su.can_view_prices())
        self.assertFalse(su.can_order())

    def test_staff_who_is_also_approved_customer_can_order(self):
        staff = CustomUser.objects.create_user(
            phone_number='09120001012', is_staff=True,
            first_name='ک', last_name='ک', national_code='1111111111',
        )
        staff, _ = staff.approve(price_level=2)
        self.assertTrue(staff.can_view_prices())
        self.assertTrue(staff.can_order())


class CleanValidationTests(TestCase):
    """ clean() فقط برای UX فرم ادمین؛ تضمین واقعی CheckConstraint است (کلاس بعدی) """

    def test_clean_raises_when_approved_without_complete_profile(self):
        # عمداً بدون save(): CheckConstraint دیتابیس همین ترکیب نامعتبر را رد می‌کند (تست بعدی)؛
        # اینجا فقط خودِ clean() روی یک نمونه‌ی حافظه‌ای (نه‌ذخیره‌شده) بررسی می‌شود
        user = CustomUser(phone_number='09120001013', approval_status=ApprovalStatus.APPROVED, price_level=1)
        with self.assertRaises(ValidationError) as ctx:
            user.clean()
        self.assertIn('approval_status', ctx.exception.error_dict)

    def test_clean_raises_when_approved_with_invalid_price_level(self):
        user = CustomUser(
            phone_number='09120001014', first_name='ح', last_name='ح', national_code='1234567890',
            approval_status=ApprovalStatus.APPROVED, price_level=99,
        )
        with self.assertRaises(ValidationError) as ctx:
            user.clean()
        self.assertIn('price_level', ctx.exception.error_dict)

    def test_clean_passes_for_valid_approved_user(self):
        user = _complete_profile_user(phone_number='09120001015', approval_status=ApprovalStatus.APPROVED, price_level=5)
        user.clean()  # نباید استثنا بدهد

    def test_clean_passes_for_pending_user_regardless_of_profile(self):
        user = CustomUser.objects.create_user(phone_number='09120001016')  # PENDING، بدون پروفایل
        user.clean()  # نباید استثنا بدهد؛ فقط APPROVED الزام دارد


class DatabaseConstraintTests(TestCase):
    """
    قیدهای دیتابیس: نوشتن مستقیم (بدون full_clean، مثل QuerySet.update()) هم باید رد شود —
    دقیقاً همان الگوی SiteSettingsGuestPricingTests.test_database_constraints_refuse_direct_invalid_writes
    در products/tests.py.
    """

    def setUp(self):
        self.incomplete = CustomUser.objects.create_user(phone_number='09120001017')  # بدون نام/کدملی
        self.complete = _complete_profile_user(phone_number='09120001018')

    def test_approving_incomplete_profile_directly_is_refused_by_db(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            CustomUser.objects.filter(pk=self.incomplete.pk).update(approval_status=ApprovalStatus.APPROVED, price_level=1)

    def test_approving_with_out_of_range_price_level_directly_is_refused_by_db(self):
        for bad_level in (0, 11, -1, 999):
            with self.subTest(price_level=bad_level):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    CustomUser.objects.filter(pk=self.complete.pk).update(
                        approval_status=ApprovalStatus.APPROVED, price_level=bad_level,
                    )

    def test_valid_direct_approval_write_succeeds(self):
        CustomUser.objects.filter(pk=self.complete.pk).update(approval_status=ApprovalStatus.APPROVED, price_level=3)
        self.complete.refresh_from_db()
        self.assertEqual(self.complete.approval_status, ApprovalStatus.APPROVED)

    def test_pending_or_rejected_status_never_hits_the_constraint_regardless_of_price_level(self):
        # قید فقط وقتی APPROVED است اعمال می‌شود؛ برای PENDING/REJECTED هر مقدار price_level مجاز است
        CustomUser.objects.filter(pk=self.incomplete.pk).update(approval_status=ApprovalStatus.PENDING, price_level=1)
        CustomUser.objects.filter(pk=self.incomplete.pk).update(approval_status=ApprovalStatus.REJECTED)
