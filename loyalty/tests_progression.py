"""
تست‌های Loyalty Phase 5C-2: پایش ارتقای رتبه هم‌زمان با کسب امتیاز
(loyalty/progression.py::credit_points_with_progression).

عمداً هیچ‌کدام از این تست‌ها هسته‌ی لجر (loyalty/services.py) یا سرویس اطلاع‌رسانی
(notifications/service.py) را مستقیم تغییر نمی‌دهند - فقط ارکستریتور جدید و ثبت
LoyaltyTierHistory را می‌سنجند.
"""

import itertools
from unittest import mock

from django.test import TestCase

from accounts.models import CustomUser
from loyalty import services
from loyalty.models import LoyaltyAccount, LoyaltyTier, LoyaltyTierHistory, LoyaltyTransaction
from loyalty.progression import credit_points_with_progression

_seq = itertools.count(1)


def _make_user():
    return CustomUser.objects.create_user(phone_number=f'0912079{next(_seq):04d}')


class ProgressionTestBase(TestCase):
    def setUp(self):
        super().setUp()
        LoyaltyTier.objects.all().delete()   # پاک‌سازی Seed فاز ۳C؛ نیازی به addCleanup نیست - TestCase خودش کل تراکنش را rollback می‌کند


class NoTierChangeTests(ProgressionTestBase):
    """ سناریوی ۱: افزایش امتیاز بدون عبور از هیچ آستانه - بدون رکورد تاریخچه، بدون اعلان. """

    def test_credit_within_the_same_tier_creates_no_history_and_does_not_notify(self):
        tier1 = LoyaltyTier.objects.create(title='برنزی', rank=0, threshold=100)
        user = _make_user()

        # اول برسیم به برنزی (یک ارتقای واقعی)
        credit_points_with_progression(user, 100, LoyaltyTransaction.EARN_ORDER, 'کسب اول')
        self.assertEqual(LoyaltyTierHistory.objects.count(), 1)

        with mock.patch('loyalty.progression.notify') as mocked_notify:
            credit_points_with_progression(user, 10, LoyaltyTransaction.EARN_ORDER, 'کسب دوم - بدون ارتقا')

        self.assertEqual(LoyaltyTierHistory.objects.count(), 1)   # هنوز همان یک رکورد
        mocked_notify.assert_not_called()


class FirstTierUpgradeTests(ProgressionTestBase):
    """ سناریوی ۲: ارتقا از هیچ سطحی (before_tier=None، چون هیچ سطحی با آستانه‌ی ۰ وجود ندارد) به اولین سطح. """

    def test_upgrade_from_no_tier_to_the_first_tier(self):
        tier1 = LoyaltyTier.objects.create(title='برنزی', rank=0, threshold=100)
        user = _make_user()

        with mock.patch('loyalty.progression.notify') as mocked_notify:
            loyalty_txn = credit_points_with_progression(user, 100, LoyaltyTransaction.EARN_ORDER, 'کسب اول')

        self.assertEqual(LoyaltyTierHistory.objects.count(), 1)
        history = LoyaltyTierHistory.objects.get()
        self.assertIsNone(history.old_tier)   # هیچ سطحی با آستانه‌ی صفر تعریف نشده - قبل از این، بدون سطح
        self.assertEqual(history.new_tier, tier1)
        self.assertEqual(history.triggering_transaction, loyalty_txn)
        self.assertIsNotNone(history.notified_at)

        mocked_notify.assert_called_once_with(user.phone_number, 'loyalty_tier_upgraded_customer', tier_title='برنزی')


class MultiTierJumpTests(ProgressionTestBase):
    """ سناریوی ۳: پرش چند پله‌ای در یک تراکنش - سطح میانی باید نادیده گرفته شود. """

    def test_jumping_multiple_tiers_in_a_single_credit_records_the_actual_before_and_after(self):
        tier1 = LoyaltyTier.objects.create(title='برنزی', rank=0, threshold=100)
        tier2 = LoyaltyTier.objects.create(title='نقره‌ای', rank=1, threshold=500)
        tier3 = LoyaltyTier.objects.create(title='طلایی', rank=2, threshold=2000)
        user = _make_user()

        credit_points_with_progression(user, 100, LoyaltyTransaction.EARN_ORDER, 'رسیدن به برنزی')
        self.assertEqual(LoyaltyTierHistory.objects.count(), 1)

        # یک کسب بزرگ که مستقیم از برنزی به طلایی می‌رود - نقره‌ای هرگز رد نمی‌شود
        with mock.patch('loyalty.progression.notify'):
            credit_points_with_progression(user, 1900, LoyaltyTransaction.EARN_ACTION, 'کسب بزرگ')

        self.assertEqual(LoyaltyTierHistory.objects.count(), 2)
        second = LoyaltyTierHistory.objects.order_by('created_at').last()
        self.assertEqual(second.old_tier, tier1)   # نه نقره‌ای
        self.assertEqual(second.new_tier, tier3)


class DebitDoesNotTriggerProgressionTests(ProgressionTestBase):
    """ سناریوی ۴: فقط credit می‌تواند ارتقا دهد - کسر/برگشت امتیاز هرگز تاریخچه‌ی تازه نمی‌سازد. """

    def test_debit_after_reaching_a_tier_creates_no_additional_history(self):
        tier1 = LoyaltyTier.objects.create(title='برنزی', rank=0, threshold=100)
        user = _make_user()
        credit_points_with_progression(user, 150, LoyaltyTransaction.EARN_ORDER, 'رسیدن به برنزی')
        self.assertEqual(LoyaltyTierHistory.objects.count(), 1)

        # کسر مستقیم از loyalty.services (نه از مسیر پایش‌شده) - lifetime_earned دست‌نخورده می‌ماند
        services.debit_points(user, 50, LoyaltyTransaction.REDEEM_WALLET, 'خرج تست')

        self.assertEqual(LoyaltyTierHistory.objects.count(), 1)   # بدون رکورد تازه
        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.lifetime_earned, 150)   # دست‌نخورده
        self.assertEqual(services.get_dynamic_tier_for_user(user), tier1)   # سطح همچنان همان است


class NotificationFailureIsolationTests(ProgressionTestBase):
    """ سناریوی ۵: خطای سرویس اعلان هرگز تراکنش کسب امتیاز را رول‌بک/متوقف نمی‌کند. """

    def test_notify_exception_does_not_roll_back_the_credit_or_leak_out(self):
        tier1 = LoyaltyTier.objects.create(title='برنزی', rank=0, threshold=100)
        user = _make_user()

        with mock.patch('loyalty.progression.notify', side_effect=RuntimeError('سرویس پیامک قطع است')):
            loyalty_txn = credit_points_with_progression(user, 100, LoyaltyTransaction.EARN_ORDER, 'کسب با خطای پیامک')

        # هیچ استثنایی به بیرون نشت نکرد (وگرنه خط بالا خودش تست را با Error می‌شکست) و مقدار درست برگشت
        self.assertEqual(loyalty_txn.amount, 100)

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 100)
        self.assertEqual(account.lifetime_earned, 100)   # کسب امتیاز کاملاً معتبر و ثبت‌شده است

        history = LoyaltyTierHistory.objects.get()
        self.assertEqual(history.new_tier, tier1)
        self.assertIsNone(history.notified_at)   # چون notify قبل از رسیدن به این خط استثنا داد
