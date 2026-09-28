"""
تست‌های Loyalty Phase 4A: موتور تبدیل امتیاز به کیف‌پول (loyalty/redemption.py).

عمداً هیچ‌کدام از این تست‌ها هسته‌ی لجر وفاداری (loyalty/services.py) یا هسته‌ی کیف‌پول
(wallet/services.py) را مستقیم تغییر نمی‌دهند/نمی‌شکنند - فقط ارکستریتور جدید و رفتار
تنظیمات SiteSettings را می‌سنجند.
"""

import threading
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from accounts.models import CustomUser
from loyalty import services
from loyalty.exceptions import IdempotencyKeyConflictError, InsufficientPointsError
from loyalty.models import LoyaltyAccount, LoyaltyTransaction
from loyalty.redemption import RedemptionValidationError, redeem_points_to_wallet
from products.models import SiteSettings
from wallet.models import Wallet, WalletTransaction

_seq = 0


def _make_user():
    global _seq
    _seq += 1
    return CustomUser.objects.create_user(phone_number=f'0912071{_seq:04d}')


class RedemptionTestBase(TestCase):
    """ هم‌الگوی accounts/tests_loyalty_display.py::LoyaltyDashboardDisplayTestBase - کش SiteSettings
    را صریح ریست می‌کند تا بین تست‌ها/فرایندهای موازی هرگز مقدار قدیمی سرریز نکند. """

    def setUp(self):
        super().setUp()
        cache.delete(SiteSettings.CACHE_KEY)
        SiteSettings.load().save()   # ردیف تک‌نمونه‌ای با مقادیر پیش‌فرض تازه (نرخ ۱۰۰، حداقل ۵۰، سقف تراکنش ۵۰۰، سقف روزانه ۱۰۰۰)
        self.addCleanup(self._reset_settings)

    def _reset_settings(self):
        cache.delete(SiteSettings.CACHE_KEY)

    def _give_points(self, user, amount):
        return services.credit_points(user, amount, LoyaltyTransaction.EARN_ORDER, 'کسب تست')


class SuccessfulRedemptionTests(RedemptionTestBase):
    def test_successful_redemption_debits_loyalty_and_credits_wallet_correctly(self):
        user = _make_user()
        self._give_points(user, 300)

        loyalty_txn, wallet_txn = redeem_points_to_wallet(user, 100, idempotency_key='redeem-1')

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 200)
        self.assertEqual(account.lifetime_redeemed, 100)
        self.assertEqual(loyalty_txn.amount, -100)
        self.assertEqual(loyalty_txn.transaction_type, LoyaltyTransaction.REDEEM_WALLET)

        wallet = Wallet.objects.get(user=user)
        self.assertEqual(wallet.balance, 100 * 100)   # نرخ پیش‌فرض ۱۰۰ تومان/امتیاز
        self.assertEqual(wallet_txn.amount, 100 * 100)
        self.assertEqual(wallet_txn.kind, WalletTransaction.KIND_LOYALTY_REDEEM)

    def test_lifetime_earned_never_changes_on_redemption(self):
        user = _make_user()
        self._give_points(user, 300)
        redeem_points_to_wallet(user, 100, idempotency_key='redeem-2')

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.lifetime_earned, 300)   # دست‌نخورده

    def test_wallet_transaction_references_the_loyalty_transaction(self):
        user = _make_user()
        self._give_points(user, 300)
        loyalty_txn, wallet_txn = redeem_points_to_wallet(user, 100, idempotency_key='redeem-3')

        self.assertEqual(wallet_txn.reference_type, 'loyalty_transaction')
        self.assertEqual(wallet_txn.reference_id, loyalty_txn.pk)

    def test_conversion_uses_the_configured_rate_not_a_hardcoded_one(self):
        user = _make_user()
        self._give_points(user, 300)
        settings_obj = SiteSettings.load()
        settings_obj.loyalty_redeem_toman_per_point = 250
        settings_obj.save()

        _, wallet_txn = redeem_points_to_wallet(user, 100, idempotency_key='redeem-rate')
        self.assertEqual(wallet_txn.amount, 100 * 250)

    def test_exact_minimum_boundary_succeeds(self):
        user = _make_user()
        self._give_points(user, 300)
        loyalty_txn, _ = redeem_points_to_wallet(user, 50, idempotency_key='redeem-min-boundary')   # حداقل پیش‌فرض
        self.assertEqual(loyalty_txn.amount, -50)

    def test_exact_max_per_transaction_boundary_succeeds(self):
        user = _make_user()
        self._give_points(user, 1000)
        loyalty_txn, _ = redeem_points_to_wallet(user, 500, idempotency_key='redeem-max-boundary')   # سقف پیش‌فرض هر تراکنش
        self.assertEqual(loyalty_txn.amount, -500)


class ValidationErrorTests(RedemptionTestBase):
    def test_below_minimum_points_is_rejected_without_touching_the_database(self):
        user = _make_user()
        self._give_points(user, 300)
        with self.assertRaises(RedemptionValidationError):
            redeem_points_to_wallet(user, 10, idempotency_key='redeem-below-min')

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 300)   # هیچ کسری اعمال نشد
        self.assertFalse(Wallet.objects.filter(user=user).exists())

    def test_above_max_per_transaction_is_rejected(self):
        user = _make_user()
        self._give_points(user, 1000)
        with self.assertRaises(RedemptionValidationError):
            redeem_points_to_wallet(user, 600, idempotency_key='redeem-above-max')

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 1000)

    def test_insufficient_points_error_and_full_rollback(self):
        user = _make_user()
        self._give_points(user, 50)   # فقط ۵۰ - کمتر از درخواست ۶۰
        with self.assertRaises(InsufficientPointsError):
            redeem_points_to_wallet(user, 60, idempotency_key='redeem-insufficient-2')

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 50)   # دست‌نخورده
        self.assertFalse(Wallet.objects.filter(user=user).exists())


class DailyCapTests(RedemptionTestBase):
    def test_daily_cap_exceeded_rolls_back_the_debit(self):
        user = _make_user()
        self._give_points(user, 2000)
        redeem_points_to_wallet(user, 500, idempotency_key='redeem-day-1')
        redeem_points_to_wallet(user, 500, idempotency_key='redeem-day-2')   # جمع تا اینجا: ۱۰۰۰ = سقف روزانه

        with self.assertRaises(RedemptionValidationError):
            redeem_points_to_wallet(user, 50, idempotency_key='redeem-day-3')   # عبور از سقف

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 1000)   # کسر تراکنش سوم رول‌بک شد
        self.assertEqual(account.lifetime_redeemed, 1000)
        wallet = Wallet.objects.get(user=user)
        self.assertEqual(wallet.balance, 1000 * 100)   # چیزی برای تراکنش سوم شارژ نشد

    def test_daily_cap_exactly_reached_succeeds(self):
        user = _make_user()
        self._give_points(user, 2000)
        redeem_points_to_wallet(user, 500, idempotency_key='redeem-day-a')
        loyalty_txn, _ = redeem_points_to_wallet(user, 500, idempotency_key='redeem-day-b')   # دقیقاً می‌رسد به سقف، رد نمی‌شود
        self.assertEqual(loyalty_txn.amount, -500)

    def test_daily_cap_uses_local_timezone_and_resets_for_a_new_day(self):
        user = _make_user()
        self._give_points(user, 2000)
        redeem_points_to_wallet(user, 500, idempotency_key='redeem-day-c')
        redeem_points_to_wallet(user, 500, idempotency_key='redeem-day-d')

        yesterday = timezone.localtime() - timezone.timedelta(days=1)
        LoyaltyTransaction.objects.filter(idempotency_key='redeem-day-c').update(created_at=yesterday)
        LoyaltyTransaction.objects.filter(idempotency_key='redeem-day-d').update(created_at=yesterday)

        loyalty_txn, _ = redeem_points_to_wallet(user, 500, idempotency_key='redeem-day-e')
        self.assertEqual(loyalty_txn.amount, -500)   # روز جدید - سقف دیروز اثری ندارد


class RollbackOnWalletFailureTests(RedemptionTestBase):
    def test_full_rollback_of_loyalty_ledger_if_wallet_credit_fails(self):
        user = _make_user()
        self._give_points(user, 300)

        with mock.patch('loyalty.redemption.credit_wallet', side_effect=RuntimeError('خطای فرضی کیف‌پول')):
            with self.assertRaises(RuntimeError):
                redeem_points_to_wallet(user, 100, idempotency_key='redeem-wallet-fail')

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 300)   # کسر امتیاز هم رول‌بک شد
        self.assertEqual(account.lifetime_redeemed, 0)
        self.assertFalse(LoyaltyTransaction.objects.filter(idempotency_key='redeem-wallet-fail').exists())
        self.assertFalse(Wallet.objects.filter(user=user).exists())


class IdempotencyTests(RedemptionTestBase):
    def test_sequential_replay_returns_the_same_pair_without_double_effect(self):
        user = _make_user()
        self._give_points(user, 300)

        first_loyalty, first_wallet = redeem_points_to_wallet(user, 100, idempotency_key='redeem-replay')
        second_loyalty, second_wallet = redeem_points_to_wallet(user, 100, idempotency_key='redeem-replay')

        self.assertEqual(first_loyalty.pk, second_loyalty.pk)
        self.assertEqual(first_wallet.pk, second_wallet.pk)

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 200)   # فقط یک‌بار کسر شد
        wallet = Wallet.objects.get(user=user)
        self.assertEqual(wallet.balance, 100 * 100)   # فقط یک‌بار شارژ شد
        self.assertEqual(WalletTransaction.objects.filter(reference_type='loyalty_transaction',
                                                            reference_id=first_loyalty.pk).count(), 1)

    def test_replay_after_daily_cap_is_later_filled_still_succeeds(self):
        """ بازپخش نباید فقط به‌خاطر اینکه سقف روزانه بعداً توسط تراکنش‌های دیگر پر شده، رد شود. """
        user = _make_user()
        self._give_points(user, 2000)
        redeem_points_to_wallet(user, 100, idempotency_key='redeem-replay-cap')
        redeem_points_to_wallet(user, 500, idempotency_key='redeem-fill-1')
        redeem_points_to_wallet(user, 400, idempotency_key='redeem-fill-2')   # جمع روز: ۱۰۰۰ = سقف

        loyalty_txn, wallet_txn = redeem_points_to_wallet(user, 100, idempotency_key='redeem-replay-cap')
        self.assertEqual(loyalty_txn.amount, -100)   # بازپخش موفق، نه RedemptionValidationError

    def test_idempotency_conflict_error_for_different_points(self):
        user = _make_user()
        self._give_points(user, 300)
        redeem_points_to_wallet(user, 100, idempotency_key='redeem-conflict-points')

        with self.assertRaises(IdempotencyKeyConflictError):
            redeem_points_to_wallet(user, 150, idempotency_key='redeem-conflict-points')

    def test_idempotency_conflict_error_for_a_different_user(self):
        user_a = _make_user()
        user_b = _make_user()
        self._give_points(user_a, 300)
        self._give_points(user_b, 300)
        redeem_points_to_wallet(user_a, 100, idempotency_key='redeem-conflict-user')

        with self.assertRaises(IdempotencyKeyConflictError):
            redeem_points_to_wallet(user_b, 100, idempotency_key='redeem-conflict-user')

        # کیف‌پول کاربر دوم نباید از تعارض کلید کاربر اول شارژ شده باشد
        self.assertFalse(Wallet.objects.filter(user=user_b).exists())


class ConcurrencyTests(TransactionTestCase):
    """ هم‌الگوی loyalty/tests.py::ConcurrencyTests - نخ‌های واقعی، هرکدام اتصال دیتابیس مستقل خودش. """

    def setUp(self):
        super().setUp()
        cache.delete(SiteSettings.CACHE_KEY)
        SiteSettings.load().save()

    def tearDown(self):
        cache.delete(SiteSettings.CACHE_KEY)
        super().tearDown()

    def run_threads(self, jobs):
        barrier = threading.Barrier(len(jobs))
        results = [None] * len(jobs)

        def worker(index, job):
            try:
                barrier.wait(timeout=30)
                results[index] = job()
            except BaseException as error:   # noqa: BLE001
                results[index] = error
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=(i, job)) for i, job in enumerate(jobs)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertFalse(any(t.is_alive() for t in threads), 'یک نخ گیر کرد (احتمال deadlock)')
        return results

    def test_concurrent_redemption_with_the_same_key_creates_exactly_one_pair(self):
        user = CustomUser.objects.create_user(phone_number='09120711111')
        services.credit_points(user, 300, LoyaltyTransaction.EARN_ORDER, 'موجودی اولیه')

        jobs = [
            (lambda: redeem_points_to_wallet(user, 100, idempotency_key='concurrent-redeem-1'))
            for _ in range(10)
        ]
        results = self.run_threads(jobs)

        unexpected = [r for r in results if not isinstance(r, tuple)]
        self.assertEqual(unexpected, [], f'نتایج غیرمنتظره: {unexpected}')

        loyalty_pks = {r[0].pk for r in results}
        wallet_pks = {r[1].pk for r in results}
        self.assertEqual(len(loyalty_pks), 1)
        self.assertEqual(len(wallet_pks), 1)

        self.assertEqual(LoyaltyTransaction.objects.filter(idempotency_key='concurrent-redeem-1').count(), 1)
        self.assertEqual(
            WalletTransaction.objects.filter(reference_type='loyalty_transaction', reference_id=loyalty_pks.pop()).count(), 1,
        )

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 200)   # فقط یک‌بار کسر شد
        wallet = Wallet.objects.get(user=user)
        self.assertEqual(wallet.balance, 100 * 100)   # فقط یک‌بار شارژ شد
