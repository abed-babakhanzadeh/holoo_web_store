"""
تست‌های Phase 1: هسته‌ی حسابداری و تراکنش‌های باشگاه مشتریان (loyalty.models/loyalty.services).

عمداً هیچ اتصالی به سیگنال‌های پرداخت/مرجوعی یا Tier جاری (accounts.models.CustomUser) تست
نمی‌شود - خارج از محدوده‌ی این فاز (نگاه کنید سربرگ loyalty/services.py). فقط دو عملیات
هسته‌ای: credit_points/debit_points و مدل‌های دفترکل.
"""

import threading

from django.contrib import admin as django_admin
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from accounts.models import CustomUser

from . import services
from .exceptions import IdempotencyKeyConflictError, InsufficientPointsError, LedgerImmutableError
from .models import LoyaltyAccount, LoyaltyTransaction

_seq = 0


def _make_user():
    global _seq
    _seq += 1
    return CustomUser.objects.create_user(phone_number=f'0912070{_seq:04d}')


class LoyaltyAccountCreationTests(TestCase):
    def test_get_or_create_for_user_creates_a_fresh_account(self):
        user = _make_user()
        self.assertFalse(LoyaltyAccount.objects.filter(user=user).exists())
        account = LoyaltyAccount.get_or_create_for_user(user)
        self.assertEqual(account.current_balance, 0)
        self.assertEqual(account.lifetime_earned, 0)
        self.assertEqual(account.lifetime_redeemed, 0)
        self.assertTrue(LoyaltyAccount.objects.filter(user=user).exists())

    def test_get_or_create_for_user_is_idempotent(self):
        user = _make_user()
        first = LoyaltyAccount.get_or_create_for_user(user)
        second = LoyaltyAccount.get_or_create_for_user(user)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(LoyaltyAccount.objects.filter(user=user).count(), 1)

    def test_credit_points_creates_the_account_implicitly(self):
        user = _make_user()
        services.credit_points(user, 50, LoyaltyTransaction.EARN_ORDER, 'سفارش تست')
        self.assertTrue(LoyaltyAccount.objects.filter(user=user).exists())


class CreditPointsTests(TestCase):
    def setUp(self):
        self.user = _make_user()

    def test_credit_increases_balance_and_lifetime_earned(self):
        txn = services.credit_points(self.user, 100, LoyaltyTransaction.EARN_ORDER, 'سفارش #۱')
        account = LoyaltyAccount.objects.get(user=self.user)
        self.assertEqual(account.current_balance, 100)
        self.assertEqual(account.lifetime_earned, 100)
        self.assertEqual(account.lifetime_redeemed, 0)
        self.assertEqual(txn.amount, 100)
        self.assertEqual(txn.balance_after, 100)
        self.assertEqual(txn.remaining_amount, 100)
        self.assertEqual(txn.transaction_type, LoyaltyTransaction.EARN_ORDER)

    def test_multiple_credits_are_summed(self):
        services.credit_points(self.user, 100, LoyaltyTransaction.EARN_ORDER, 'سفارش #۱')
        services.credit_points(self.user, 30, LoyaltyTransaction.ADMIN_CREDIT, 'هدیه')
        account = LoyaltyAccount.objects.get(user=self.user)
        self.assertEqual(account.current_balance, 130)
        self.assertEqual(account.lifetime_earned, 130)

    def test_non_positive_amount_is_rejected(self):
        for bad in (0, -5):
            with self.subTest(amount=bad), self.assertRaises(ValueError):
                services.credit_points(self.user, bad, LoyaltyTransaction.EARN_ORDER, 'نامعتبر')

    def test_empty_reason_is_rejected(self):
        with self.assertRaises(ValueError):
            services.credit_points(self.user, 10, LoyaltyTransaction.EARN_ORDER, '   ')

    def test_idempotency_key_prevents_double_credit(self):
        key = 'order-123-earn'
        first = services.credit_points(self.user, 50, LoyaltyTransaction.EARN_ORDER, 'سفارش #۱', idempotency_key=key)
        second = services.credit_points(self.user, 50, LoyaltyTransaction.EARN_ORDER, 'سفارش #۱', idempotency_key=key)
        self.assertEqual(first.pk, second.pk)
        account = LoyaltyAccount.objects.get(user=self.user)
        self.assertEqual(account.current_balance, 50)
        self.assertEqual(account.lifetime_earned, 50)
        self.assertEqual(LoyaltyTransaction.objects.filter(idempotency_key=key).count(), 1)


class DebitPointsTests(TestCase):
    def setUp(self):
        self.user = _make_user()
        services.credit_points(self.user, 100, LoyaltyTransaction.EARN_ORDER, 'موجودی اولیه')

    def test_debit_decreases_balance_and_increases_lifetime_redeemed_without_touching_lifetime_earned(self):
        txn = services.debit_points(self.user, 40, LoyaltyTransaction.REDEEM_WALLET, 'تبدیل به کیف پول')
        account = LoyaltyAccount.objects.get(user=self.user)
        self.assertEqual(account.current_balance, 60)
        self.assertEqual(account.lifetime_redeemed, 40)
        self.assertEqual(account.lifetime_earned, 100)   # دست‌نخورده
        self.assertEqual(txn.amount, -40)
        self.assertEqual(txn.balance_after, 60)

    def test_debit_more_than_balance_raises_and_rolls_back(self):
        with self.assertRaises(InsufficientPointsError):
            services.debit_points(self.user, 150, LoyaltyTransaction.REDEEM_WALLET, 'بیش از موجودی')
        account = LoyaltyAccount.objects.get(user=self.user)
        self.assertEqual(account.current_balance, 100)   # دست‌نخورده
        self.assertEqual(account.lifetime_redeemed, 0)
        self.assertEqual(LoyaltyTransaction.objects.filter(account__user=self.user).count(), 1)   # فقط کسبِ اولیه

    def test_non_positive_amount_is_rejected(self):
        with self.assertRaises(ValueError):
            services.debit_points(self.user, 0, LoyaltyTransaction.REDEEM_WALLET, 'نامعتبر')

    def test_idempotency_key_prevents_double_debit(self):
        key = 'redeem-xyz'
        first = services.debit_points(self.user, 30, LoyaltyTransaction.REDEEM_WALLET, 'تبدیل اول', idempotency_key=key)
        second = services.debit_points(self.user, 30, LoyaltyTransaction.REDEEM_WALLET, 'تبدیل اول', idempotency_key=key)
        self.assertEqual(first.pk, second.pk)
        account = LoyaltyAccount.objects.get(user=self.user)
        self.assertEqual(account.current_balance, 70)   # فقط یک‌بار کسر شده


class IdempotencyKeyConflictTests(TestCase):
    """
    محور ممیزی فاز ۱ - بند ۱: کلید ضدتکرار یکتاست (unique=True روی خودِ فیلد، نه ترکیبی)؛ اگر
    همان کلید با amount/transaction_type متفاوتی دوباره فرستاده شود، رکورد قبلی بی‌سروصدا
    برگردانده نمی‌شود - IdempotencyKeyConflictError صادر می‌شود (services.py::_existing_idempotent_transaction).
    """

    def setUp(self):
        self.user = _make_user()

    def test_credit_with_same_key_but_different_amount_raises_conflict(self):
        services.credit_points(self.user, 100, LoyaltyTransaction.EARN_ORDER, 'اول', idempotency_key='k-amount')
        with self.assertRaises(IdempotencyKeyConflictError):
            services.credit_points(self.user, 500, LoyaltyTransaction.EARN_ORDER, 'دوم', idempotency_key='k-amount')
        account = LoyaltyAccount.objects.get(user=self.user)
        self.assertEqual(account.current_balance, 100)   # فقط تلاش اول اعمال شده؛ تلاش دوم هیچ اثری نداشته

    def test_credit_with_same_key_but_different_transaction_type_raises_conflict(self):
        services.credit_points(self.user, 100, LoyaltyTransaction.EARN_ORDER, 'اول', idempotency_key='k-type')
        with self.assertRaises(IdempotencyKeyConflictError):
            services.credit_points(self.user, 100, LoyaltyTransaction.ADMIN_CREDIT, 'دوم', idempotency_key='k-type')

    def test_debit_with_same_key_but_different_amount_raises_conflict(self):
        services.credit_points(self.user, 1000, LoyaltyTransaction.EARN_ORDER, 'موجودی اولیه')
        services.debit_points(self.user, 50, LoyaltyTransaction.REDEEM_WALLET, 'اول', idempotency_key='k-debit')
        with self.assertRaises(IdempotencyKeyConflictError):
            services.debit_points(self.user, 90, LoyaltyTransaction.REDEEM_WALLET, 'دوم', idempotency_key='k-debit')
        account = LoyaltyAccount.objects.get(user=self.user)
        self.assertEqual(account.current_balance, 950)   # ۱۰۰۰ - ۵۰ (فقط تلاش اول)

    def test_matching_replay_still_returns_the_same_transaction(self):
        """ یادآوری تمایز: کلید تکراری با پارامترهای *یکسان* هنوز خطا نمی‌دهد - رفتار ایدمپوتنت است. """
        first = services.credit_points(self.user, 100, LoyaltyTransaction.EARN_ORDER, 'اول', idempotency_key='k-same')
        second = services.credit_points(self.user, 100, LoyaltyTransaction.EARN_ORDER, 'دوم', idempotency_key='k-same')
        self.assertEqual(first.pk, second.pk)


class ReverseTransactionTypeTests(TestCase):
    """
    محور ممیزی فاز ۱ - بند ۳: تأیید صریح رفتار فعلی - REVERSE هیچ رفتار ویژه‌ای در debit_points
    ندارد (نگاه کنید سربرگ services.debit_points). تأثیر واقعی مرجوعی روی Tier/lifetime_earned
    عمداً به فاز ۲ موکول شده؛ این تست فقط رفتار *فعلی* (نه رفتار مطلوب نهایی) را قفل می‌کند تا
    تغییر ناخواسته‌ی بی‌صدا در فاز ۲ فوراً قرمز شود.
    """

    def test_reverse_behaves_like_any_other_debit(self):
        user = _make_user()
        services.credit_points(user, 100, LoyaltyTransaction.EARN_ORDER, 'سفارشی که بعداً لغو شد')
        txn = services.debit_points(user, 100, LoyaltyTransaction.REVERSE, 'برگشت به دلیل لغو سفارش')

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(txn.amount, -100)
        self.assertEqual(account.current_balance, 0)
        self.assertEqual(account.lifetime_earned, 100)     # عمداً دست‌نخورده - تصمیم Tier به فاز ۲ موکول شده
        self.assertEqual(account.lifetime_redeemed, 100)   # REVERSE هم مثل هر debit دیگری اینجا جمع می‌شود


class AdminPermissionLockdownTests(TestCase):
    """ محور ممیزی فاز ۱ - بند ۵: has_add_permission/has_change_permission/has_delete_permission. """

    def setUp(self):
        self.superuser = CustomUser.objects.create_superuser(phone_number='09120788888')
        self.client.force_login(self.superuser)

    def test_loyaltyaccount_admin_methods_block_add_and_delete(self):
        model_admin = django_admin.site._registry[LoyaltyAccount]
        self.assertFalse(model_admin.has_add_permission(None))
        self.assertFalse(model_admin.has_delete_permission(None))

    def test_loyaltytransaction_admin_methods_block_add_change_delete(self):
        model_admin = django_admin.site._registry[LoyaltyTransaction]
        self.assertFalse(model_admin.has_add_permission(None))
        self.assertFalse(model_admin.has_change_permission(None))
        self.assertFalse(model_admin.has_delete_permission(None))

    def test_loyaltyaccount_add_url_is_forbidden_even_for_superuser(self):
        response = self.client.get(reverse('admin:loyalty_loyaltyaccount_add'))
        self.assertEqual(response.status_code, 403)

    def test_loyaltyaccount_delete_url_is_forbidden_even_for_superuser(self):
        user = _make_user()
        account = LoyaltyAccount.get_or_create_for_user(user)
        response = self.client.get(reverse('admin:loyalty_loyaltyaccount_delete', args=[account.pk]))
        self.assertEqual(response.status_code, 403)

    def test_loyaltytransaction_add_url_is_forbidden_even_for_superuser(self):
        response = self.client.get(reverse('admin:loyalty_loyaltytransaction_add'))
        self.assertEqual(response.status_code, 403)

    def test_loyaltytransaction_change_post_never_modifies_the_record(self):
        """
        نکته‌ی مهم Django: has_change_permission=False فقط ذخیره‌شدن را مسدود می‌کند، نه GET صفحه
        (چون has_view_or_change_permission برای سوپریوزر همچنان True است - دقیقاً هم‌رفتار
        wallet.admin.WalletAdmin موجود در پروژه). تضمین واقعی این است که هیچ POST ای رکورد را
        عوض نکند؛ همان چیزی که این تست می‌سنجد.
        """
        user = _make_user()
        txn = services.credit_points(user, 10, LoyaltyTransaction.EARN_ORDER, 'تست')
        original_reason = txn.reason
        url = reverse('admin:loyalty_loyaltytransaction_change', args=[txn.pk])
        response = self.client.post(url, {'reason': 'دستکاری شده'})
        self.assertIn(response.status_code, (200, 403))
        txn.refresh_from_db()
        self.assertEqual(txn.reason, original_reason)


class AdminDoubleSubmitProtectionTests(TestCase):
    """
    محور ممیزی فاز ۱ - بند ۴: فرم میانی اعطا/کسر دستی یک adjustment_token مخفی و یک‌بارمصرف دارد
    که عیناً idempotency_key سرویس می‌شود - شبیه‌سازی دقیق دابل‌کلیک واقعی: همان توکن، دو POST جدا.
    """

    def setUp(self):
        self.superuser = CustomUser.objects.create_superuser(phone_number='09120777777')
        self.client.force_login(self.superuser)
        self.user = _make_user()
        self.account = LoyaltyAccount.get_or_create_for_user(self.user)

    def _get_token(self, url):
        response = self.client.get(url)
        return response.context['form']['adjustment_token'].value()

    def test_double_submitting_the_credit_form_only_credits_once(self):
        url = reverse('admin:loyalty_loyaltyaccount_credit', args=[self.account.pk])
        token = self._get_token(url)
        payload = {'amount': '100', 'reason': 'تست دابل‌ساب‌میت', 'adjustment_token': token}

        first = self.client.post(url, payload)
        second = self.client.post(url, payload)   # همان توکن - دقیقاً شبیه دابل‌کلیک واقعی

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        account = LoyaltyAccount.objects.get(pk=self.account.pk)
        self.assertEqual(account.current_balance, 100)   # نه ۲۰۰
        self.assertEqual(LoyaltyTransaction.objects.filter(account=account).count(), 1)

    def test_double_submitting_the_debit_form_only_debits_once(self):
        services.credit_points(self.user, 200, LoyaltyTransaction.EARN_ORDER, 'موجودی اولیه')
        url = reverse('admin:loyalty_loyaltyaccount_debit', args=[self.account.pk])
        token = self._get_token(url)
        payload = {'amount': '50', 'reason': 'تست دابل‌ساب‌میت', 'adjustment_token': token}

        first = self.client.post(url, payload)
        second = self.client.post(url, payload)

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        account = LoyaltyAccount.objects.get(pk=self.account.pk)
        self.assertEqual(account.current_balance, 150)   # ۲۰۰ - ۵۰ فقط یک‌بار
        self.assertEqual(
            LoyaltyTransaction.objects.filter(account=account, transaction_type=LoyaltyTransaction.ADMIN_DEBIT).count(), 1,
        )

    def test_reloading_the_form_before_resubmitting_issues_a_fresh_token_and_credits_again(self):
        """ رفتار درست/مقابل: بازکردنِ دوباره‌ی صفحه (نه دابل‌کلیک) توکن تازه می‌دهد و اعطای واقعی جدید مجاز است. """
        url = reverse('admin:loyalty_loyaltyaccount_credit', args=[self.account.pk])
        token1 = self._get_token(url)
        self.client.post(url, {'amount': '100', 'reason': 'اول', 'adjustment_token': token1})

        token2 = self._get_token(url)
        self.assertNotEqual(token1, token2)
        self.client.post(url, {'amount': '100', 'reason': 'دوم', 'adjustment_token': token2})

        account = LoyaltyAccount.objects.get(pk=self.account.pk)
        self.assertEqual(account.current_balance, 200)   # دو اعطای واقعی و جدا


class DatabaseConstraintTests(TestCase):
    def test_negative_current_balance_is_rejected_at_database_level(self):
        user = _make_user()
        account = LoyaltyAccount.get_or_create_for_user(user)
        with self.assertRaises(IntegrityError), transaction.atomic():
            LoyaltyAccount.objects.filter(pk=account.pk).update(current_balance=-1)

    def test_negative_balance_after_is_rejected_at_database_level(self):
        user = _make_user()
        txn = services.credit_points(user, 10, LoyaltyTransaction.EARN_ORDER, 'تست')
        with self.assertRaises(IntegrityError), transaction.atomic():
            LoyaltyTransaction.objects.filter(pk=txn.pk).update(balance_after=-1)


class LedgerImmutabilityTests(TestCase):
    def test_editing_an_existing_transaction_is_rejected(self):
        user = _make_user()
        txn = services.credit_points(user, 20, LoyaltyTransaction.EARN_ORDER, 'تست')
        txn.reason = 'دستکاری'
        with self.assertRaises(LedgerImmutableError):
            txn.save()

    def test_deleting_a_transaction_is_rejected(self):
        user = _make_user()
        txn = services.credit_points(user, 20, LoyaltyTransaction.EARN_ORDER, 'تست')
        with self.assertRaises(LedgerImmutableError):
            txn.delete()
        self.assertTrue(LoyaltyTransaction.objects.filter(pk=txn.pk).exists())


class ConcurrencyTests(TransactionTestCase):
    """
    اثبات عدم Double-Spend با چند نخِ واقعی، هرکدام با اتصال دیتابیس مستقل خودش، هم‌زمان با یک
    Barrier شروع می‌شوند (هم‌الگوی orders/tests_coupon_concurrency.py::CouponConcurrencyBase.run_threads).
    """

    def run_threads(self, jobs):
        barrier = threading.Barrier(len(jobs))
        results = [None] * len(jobs)

        def worker(index, job):
            try:
                barrier.wait(timeout=30)
                results[index] = job()
            except BaseException as error:   # noqa: BLE001 - نتیجه‌ی استثنا هم ثبت می‌شود
                results[index] = error
            finally:
                connection.close()   # اتصال مخصوص همین نخ

        threads = [threading.Thread(target=worker, args=(i, job)) for i, job in enumerate(jobs)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertFalse(any(t.is_alive() for t in threads), 'یک نخ گیر کرد (احتمال deadlock)')
        return results

    def test_concurrent_debits_never_overdraw_the_account(self):
        user = CustomUser.objects.create_user(phone_number='09120799999')
        services.credit_points(user, 100, LoyaltyTransaction.EARN_ORDER, 'موجودی اولیه')

        jobs = [
            (lambda: services.debit_points(user, 15, LoyaltyTransaction.REDEEM_WALLET, 'خرج هم‌زمان'))
            for _ in range(10)
        ]
        results = self.run_threads(jobs)

        succeeded = [r for r in results if isinstance(r, LoyaltyTransaction)]
        failed = [r for r in results if isinstance(r, InsufficientPointsError)]
        unexpected = [r for r in results if not isinstance(r, (LoyaltyTransaction, InsufficientPointsError))]

        self.assertEqual(unexpected, [], f'نتایج غیرمنتظره: {unexpected}')
        self.assertEqual(len(succeeded), 6)   # ۱۰۰ ÷ ۱۵ = ۶ موفق؛ هفتمی با کسری موجودی رد می‌شود
        self.assertEqual(len(failed), 4)

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 10)   # ۱۰۰ - ۶×۱۵
        self.assertEqual(account.lifetime_redeemed, 90)
        self.assertGreaterEqual(account.current_balance, 0)   # هرگز منفی نشده

    def test_concurrent_credits_with_the_same_idempotency_key_apply_only_once(self):
        user = CustomUser.objects.create_user(phone_number='09120799998')
        jobs = [
            (lambda: services.credit_points(
                user, 50, LoyaltyTransaction.EARN_ORDER, 'سفارش هم‌زمان', idempotency_key='concurrent-order-1',
            ))
            for _ in range(8)
        ]
        results = self.run_threads(jobs)
        unexpected = [r for r in results if not isinstance(r, LoyaltyTransaction)]
        self.assertEqual(unexpected, [], f'نتایج غیرمنتظره: {unexpected}')

        pks = {r.pk for r in results}
        self.assertEqual(len(pks), 1)   # همه به یک رکورد واحد اشاره می‌کنند

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 50)   # فقط یک‌بار اعمال شده، نه ۸ بار
        self.assertEqual(account.lifetime_earned, 50)
        self.assertEqual(LoyaltyTransaction.objects.filter(idempotency_key='concurrent-order-1').count(), 1)

    def test_concurrent_debits_with_the_same_idempotency_key_and_balance_for_exactly_one_never_error(self):
        """
        رگرسیون مستقیم روی باگی که loyalty/tests_cancellation.py::CancellationConcurrencyTests
        کشف کرد: موجودی اولیه فقط برای *یک* کسر کافی است و همه‌ی نخ‌ها همان idempotency_key را
        دارند (دقیقاً شکل «بازگشت لغو سفارش»). پیش از انتقال چک ایدمپوتنسی به داخل قفل، نخ‌های
        بازنده InsufficientPointsError می‌گرفتند؛ حالا باید همه بدون خطا همان رکورد برنده را
        برگردانند.
        """
        user = CustomUser.objects.create_user(phone_number='09120799997')
        services.credit_points(user, 100, LoyaltyTransaction.EARN_ORDER, 'موجودی اولیه')

        jobs = [
            (lambda: services.debit_points(
                user, 100, LoyaltyTransaction.REVERSE, 'برگشت هم‌زمان',
                idempotency_key='concurrent-reverse-1',
            ))
            for _ in range(8)
        ]
        results = self.run_threads(jobs)

        unexpected = [r for r in results if not isinstance(r, LoyaltyTransaction)]
        self.assertEqual(unexpected, [], f'نتایج غیرمنتظره (باید همه رکورد باشند، نه استثنا): {unexpected}')

        pks = {r.pk for r in results}
        self.assertEqual(len(pks), 1)   # همه به یک رکورد واحد اشاره می‌کنند

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 0)     # نه منفی، نه دوبار کسر شده
        self.assertEqual(account.lifetime_redeemed, 100)
        self.assertEqual(LoyaltyTransaction.objects.filter(idempotency_key='concurrent-reverse-1').count(), 1)
