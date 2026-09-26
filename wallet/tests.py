"""
تست‌های هسته‌ی مالی کیف پول (Wallet Phase 1): invariant ها، لایه‌ی سرویس، تطابق دفترکل، و
هم‌زمانی واقعی (Double Spending). هیچ ویو/URL ای در این فاز وجود ندارد؛ همه‌چیز مستقیم روی
مدل‌ها و wallet/services.py تست می‌شود.
"""

import itertools
import threading
from decimal import Decimal

from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase

from accounts.models import CustomUser
from notifications.models import Notification

from . import services
from .models import Wallet, WalletTransaction, WithdrawalRequest

_seq = itertools.count(1)


def make_wallet(balance=0, reserved=0):
    user = CustomUser.objects.create_user(phone_number=f'0912002{next(_seq):04d}')
    return Wallet.objects.create(user=user, balance=balance, reserved_balance=reserved)


class WalletModelConstraintTests(TestCase):
    """ قیدهای دیتابیسی؛ الگوی assertRaises(IntegrityError) + transaction.atomic() هم‌شکل products/tests.py """

    def test_balance_cannot_go_negative_at_db_level(self):
        wallet = make_wallet(balance=100)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Wallet.objects.filter(pk=wallet.pk).update(balance=-1)

    def test_reserved_balance_cannot_go_negative_at_db_level(self):
        wallet = make_wallet(balance=100)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Wallet.objects.filter(pk=wallet.pk).update(reserved_balance=-1)

    def test_reserved_balance_cannot_exceed_balance_at_db_level(self):
        wallet = make_wallet(balance=100, reserved=0)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Wallet.objects.filter(pk=wallet.pk).update(reserved_balance=101)

    def test_reserved_balance_equal_to_balance_is_allowed(self):
        wallet = make_wallet(balance=100, reserved=0)
        Wallet.objects.filter(pk=wallet.pk).update(reserved_balance=100)
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, 0)

    def test_available_balance_property(self):
        wallet = make_wallet(balance=1000, reserved=300)
        self.assertEqual(wallet.available_balance, 700)

    def test_wallet_transaction_is_immutable(self):
        wallet = make_wallet(balance=100)
        txn = WalletTransaction.objects.create(
            wallet=wallet, amount=100, kind=WalletTransaction.KIND_TOPUP, balance_after=100,
        )
        txn.description = 'دستکاری بعدی'
        with self.assertRaises(ValueError):
            txn.save()
        with self.assertRaises(ValueError):
            txn.delete()

    def test_withdrawal_request_rejected_requires_reason_at_db_level(self):
        wallet = make_wallet(balance=1000)
        request = WithdrawalRequest.objects.create(wallet=wallet, amount=100, account_holder_snapshot='کاربر تست')
        with self.assertRaises(IntegrityError), transaction.atomic():
            WithdrawalRequest.objects.filter(pk=request.pk).update(
                status=WithdrawalRequest.STATUS_REJECTED, rejection_reason='',
            )

    def test_withdrawal_request_completed_requires_transaction_at_db_level(self):
        wallet = make_wallet(balance=1000)
        request = WithdrawalRequest.objects.create(wallet=wallet, amount=100, account_holder_snapshot='کاربر تست')
        with self.assertRaises(IntegrityError), transaction.atomic():
            WithdrawalRequest.objects.filter(pk=request.pk).update(status=WithdrawalRequest.STATUS_COMPLETED)


class WalletServicesTests(TestCase):
    def test_credit_wallet_increases_balance_and_writes_ledger(self):
        wallet = make_wallet(balance=0)
        txn = services.credit_wallet(wallet, 50000, WalletTransaction.KIND_TOPUP, reference_type='wallet_topup_request', reference_id=1)
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, 50000)
        self.assertEqual(txn.amount, 50000)
        self.assertEqual(txn.balance_after, 50000)
        self.assertEqual(txn.reference_type, 'wallet_topup_request')
        self.assertEqual(txn.reference_id, 1)

    def test_credit_wallet_rejects_non_positive_amount(self):
        wallet = make_wallet(balance=0)
        with self.assertRaises(ValueError):
            services.credit_wallet(wallet, 0, WalletTransaction.KIND_TOPUP)
        with self.assertRaises(ValueError):
            services.credit_wallet(wallet, -100, WalletTransaction.KIND_TOPUP)

    def test_debit_wallet_decreases_balance_and_writes_ledger(self):
        wallet = make_wallet(balance=100000)
        txn = services.debit_wallet(wallet, 30000, WalletTransaction.KIND_CART_PAYMENT)
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, 70000)
        self.assertEqual(txn.amount, -30000)
        self.assertEqual(txn.balance_after, 70000)

    def test_debit_wallet_raises_when_insufficient_available_balance(self):
        wallet = make_wallet(balance=10000)
        with self.assertRaises(services.InsufficientBalanceError):
            services.debit_wallet(wallet, 20000, WalletTransaction.KIND_CART_PAYMENT)
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, 10000)  # هیچ تغییری اعمال نشده

    def test_debit_wallet_respects_reserved_balance_not_just_raw_balance(self):
        """ پولی که برای یک درخواست برداشت بلوکه شده، نباید دوباره برای debit خرج شود (Double Spending) """
        wallet = make_wallet(balance=100000)
        services.reserve_withdrawal(wallet, 90000, account_holder='کاربر تست')
        wallet.refresh_from_db()
        self.assertEqual(wallet.available_balance, 10000)
        with self.assertRaises(services.InsufficientBalanceError):
            services.debit_wallet(wallet, 20000, WalletTransaction.KIND_CART_PAYMENT)

    def test_reverse_transaction_writes_compensating_entry_without_editing_original(self):
        wallet = make_wallet(balance=0)
        original = services.credit_wallet(wallet, 50000, WalletTransaction.KIND_TOPUP)
        reversal = services.reverse_transaction(original, reason='پرداخت اشتباه بود')
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, 0)
        self.assertEqual(reversal.amount, -50000)
        self.assertEqual(reversal.kind, WalletTransaction.KIND_REVERSAL)
        self.assertEqual(reversal.reference_type, 'wallet_transaction')
        self.assertEqual(reversal.reference_id, original.pk)
        original.refresh_from_db()
        self.assertEqual(original.amount, 50000)  # اصل رکورد دست‌نخورده مانده

    def test_reverse_transaction_requires_reason(self):
        wallet = make_wallet(balance=0)
        original = services.credit_wallet(wallet, 1000, WalletTransaction.KIND_TOPUP)
        with self.assertRaises(ValueError):
            services.reverse_transaction(original, reason='')


class WalletLedgerReconciliationTests(TestCase):
    """ balance باید همیشه با جمع جبری دفترکل خودش برابر باشد """

    def test_balance_matches_ledger_sum_after_mixed_operations(self):
        wallet = make_wallet(balance=0)
        services.credit_wallet(wallet, 200000, WalletTransaction.KIND_TOPUP)
        services.debit_wallet(wallet, 50000, WalletTransaction.KIND_CART_PAYMENT)
        services.credit_wallet(wallet, 30000, WalletTransaction.KIND_REFUND)
        request = services.reserve_withdrawal(wallet, 100000, account_holder='کاربر تست')
        services.approve_withdrawal(request, admin_user=None)
        services.mark_withdrawal_paid(request, admin_user=None)

        wallet.refresh_from_db()
        ledger_sum = sum((t.amount for t in wallet.transactions.all()), Decimal('0'))
        self.assertEqual(wallet.balance, ledger_sum)
        self.assertEqual(wallet.balance, 80000)          # 200000 - 50000 + 30000 - 100000
        self.assertEqual(wallet.reserved_balance, 0)      # بعد از تکمیل، رزرو آزاد شده

    def test_every_balance_mutation_has_a_matching_ledger_row(self):
        wallet = make_wallet(balance=0)
        services.credit_wallet(wallet, 10000, WalletTransaction.KIND_TOPUP)
        services.credit_wallet(wallet, 20000, WalletTransaction.KIND_TOPUP)
        self.assertEqual(wallet.transactions.count(), 2)
        balances_after = list(wallet.transactions.order_by('created_at', 'id').values_list('balance_after', flat=True))
        self.assertEqual(balances_after, [10000, 30000])


class WithdrawalLifecycleTests(TestCase):
    def test_reserve_withdrawal_blocks_reserved_without_touching_balance_and_notifies_admin(self):
        wallet = make_wallet(balance=100000)
        with self.captureOnCommitCallbacks(execute=True):
            request = services.reserve_withdrawal(
                wallet, 40000, account_holder='کاربر تست', card_number='6219861035427496', iban='IR000000000000000000000000',
            )
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, 100000)          # دست‌نخورده
        self.assertEqual(wallet.reserved_balance, 40000)
        self.assertEqual(wallet.available_balance, 60000)
        self.assertEqual(request.status, WithdrawalRequest.STATUS_PENDING)
        self.assertEqual(request.card_number_snapshot, '6219861035427496')
        self.assertEqual(Notification.objects.filter(template_key='withdrawal_requested_admin').count(), 1)
        self.assertEqual(Notification.objects.filter(template_key='withdrawal_requested_customer').count(), 1)

    def test_reserve_withdrawal_raises_when_insufficient_available_balance(self):
        wallet = make_wallet(balance=10000)
        with self.assertRaises(services.InsufficientBalanceError):
            services.reserve_withdrawal(wallet, 20000, account_holder='کاربر تست')
        wallet.refresh_from_db()
        self.assertEqual(wallet.reserved_balance, 0)

    def test_reserve_withdrawal_requires_account_holder(self):
        wallet = make_wallet(balance=100000)
        with self.assertRaises(ValueError):
            services.reserve_withdrawal(wallet, 1000, account_holder='')

    def test_approve_withdrawal_only_changes_status_not_balance(self):
        wallet = make_wallet(balance=100000)
        request = services.reserve_withdrawal(wallet, 40000, account_holder='کاربر تست')
        admin = CustomUser.objects.create_superuser(phone_number=f'0912003{next(_seq):04d}')

        approved = services.approve_withdrawal(request, admin_user=admin)

        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, 100000)              # هنوز دست‌نخورده
        self.assertEqual(wallet.reserved_balance, 40000)      # هنوز بلوکه، نه کسرشده
        self.assertEqual(approved.status, WithdrawalRequest.STATUS_APPROVED)
        self.assertIsNone(approved.transaction)               # هنوز هیچ ردیف لجری ثبت نشده
        self.assertEqual(approved.decided_by, admin)
        self.assertIsNotNone(approved.decided_at)

    def test_approve_withdrawal_from_non_pending_raises(self):
        wallet = make_wallet(balance=100000)
        request = services.reserve_withdrawal(wallet, 40000, account_holder='کاربر تست')
        services.approve_withdrawal(request, admin_user=None)
        with self.assertRaises(services.InvalidWithdrawalStateError):
            services.approve_withdrawal(request, admin_user=None)

    def test_mark_withdrawal_paid_requires_prior_approval(self):
        wallet = make_wallet(balance=100000)
        request = services.reserve_withdrawal(wallet, 40000, account_holder='کاربر تست')
        with self.assertRaises(services.InvalidWithdrawalStateError):
            services.mark_withdrawal_paid(request, admin_user=None)          # هنوز PENDING است، نه APPROVED
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, 100000)
        self.assertEqual(wallet.reserved_balance, 40000)

    def test_mark_withdrawal_paid_deducts_balance_and_reserved_and_links_transaction(self):
        wallet = make_wallet(balance=100000)
        request = services.reserve_withdrawal(wallet, 40000, account_holder='کاربر تست')
        admin = CustomUser.objects.create_superuser(phone_number=f'0912003{next(_seq):04d}')
        services.approve_withdrawal(request, admin_user=admin)

        completed = services.mark_withdrawal_paid(request, admin_user=admin)

        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, 60000)
        self.assertEqual(wallet.reserved_balance, 0)
        self.assertEqual(completed.status, WithdrawalRequest.STATUS_COMPLETED)
        self.assertIsNotNone(completed.transaction)
        self.assertEqual(completed.transaction.amount, -40000)
        self.assertEqual(completed.transaction.kind, WalletTransaction.KIND_WITHDRAWAL)
        self.assertIsNotNone(completed.paid_at)

    def test_mark_withdrawal_paid_twice_raises(self):
        wallet = make_wallet(balance=100000)
        request = services.reserve_withdrawal(wallet, 40000, account_holder='کاربر تست')
        admin = CustomUser.objects.create_superuser(phone_number=f'0912003{next(_seq):04d}')
        services.approve_withdrawal(request, admin_user=admin)
        services.mark_withdrawal_paid(request, admin_user=admin)
        with self.assertRaises(services.InvalidWithdrawalStateError):
            services.mark_withdrawal_paid(request, admin_user=admin)

    def test_reject_withdrawal_from_pending_releases_reserved_without_touching_balance(self):
        wallet = make_wallet(balance=100000)
        request = services.reserve_withdrawal(wallet, 40000, account_holder='کاربر تست')
        admin = CustomUser.objects.create_superuser(phone_number=f'0912003{next(_seq):04d}')

        rejected = services.reject_withdrawal(request, 'شماره شبا نامعتبر بود', admin_user=admin)

        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, 100000)          # پول اصلاً جایی نرفته بود
        self.assertEqual(wallet.reserved_balance, 0)      # رزرو آزاد شد
        self.assertEqual(rejected.status, WithdrawalRequest.STATUS_REJECTED)
        self.assertEqual(rejected.rejection_reason, 'شماره شبا نامعتبر بود')
        self.assertIsNone(rejected.transaction)

    def test_reject_withdrawal_from_approved_also_releases_reserved(self):
        """ رد بعد از تأیید هم مجاز است (مثلاً معلوم شد شبا غلط بوده) """
        wallet = make_wallet(balance=100000)
        request = services.reserve_withdrawal(wallet, 40000, account_holder='کاربر تست')
        services.approve_withdrawal(request, admin_user=None)

        rejected = services.reject_withdrawal(request, 'شبا نامعتبر بود', admin_user=None)

        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, 100000)
        self.assertEqual(wallet.reserved_balance, 0)
        self.assertEqual(rejected.status, WithdrawalRequest.STATUS_REJECTED)

    def test_reject_withdrawal_requires_reason(self):
        wallet = make_wallet(balance=100000)
        request = services.reserve_withdrawal(wallet, 40000, account_holder='کاربر تست')
        with self.assertRaises(ValueError):
            services.reject_withdrawal(request, '', admin_user=None)

    def test_reject_withdrawal_twice_raises(self):
        wallet = make_wallet(balance=100000)
        request = services.reserve_withdrawal(wallet, 40000, account_holder='کاربر تست')
        services.reject_withdrawal(request, 'دلیل اول', admin_user=None)
        with self.assertRaises(services.InvalidWithdrawalStateError):
            services.reject_withdrawal(request, 'دلیل دوم', admin_user=None)

    def test_reject_withdrawal_after_completed_raises(self):
        wallet = make_wallet(balance=100000)
        request = services.reserve_withdrawal(wallet, 40000, account_holder='کاربر تست')
        services.approve_withdrawal(request, admin_user=None)
        services.mark_withdrawal_paid(request, admin_user=None)
        with self.assertRaises(services.InvalidWithdrawalStateError):
            services.reject_withdrawal(request, 'خیلی دیر شد', admin_user=None)


class WithdrawalNotificationTests(TestCase):
    """
    ۴ رویداد پیامکی چرخه‌ی برداشت (Phase 3). فقط شمارش Notification.objects بر اساس
    template_key بررسی می‌شود (محتوای دقیق متن در notifications/tests.py رندر می‌شود)؛
    نکته‌ی امنیتی «بدون شبا/کارت در پیامک مشتری» با بررسی نبود این ارقام در context تضمین
    می‌شود - قالب پیام‌ها اصلاً چنین متغیرهایی را required نمی‌گیرند (نگاه کنید templates_registry.py).
    """

    def test_reserve_notifies_both_customer_and_admin(self):
        wallet = make_wallet(balance=100000)
        with self.captureOnCommitCallbacks(execute=True):
            services.reserve_withdrawal(wallet, 40000, account_holder='کاربر تست')
        self.assertEqual(Notification.objects.filter(template_key='withdrawal_requested_customer').count(), 1)
        self.assertEqual(Notification.objects.filter(template_key='withdrawal_requested_admin').count(), 1)

    def test_approve_notifies_customer(self):
        wallet = make_wallet(balance=100000)
        request = services.reserve_withdrawal(wallet, 40000, account_holder='کاربر تست')
        with self.captureOnCommitCallbacks(execute=True):
            services.approve_withdrawal(request, admin_user=None)
        self.assertEqual(Notification.objects.filter(template_key='withdrawal_approved_customer').count(), 1)

    def test_mark_paid_notifies_customer(self):
        wallet = make_wallet(balance=100000)
        request = services.reserve_withdrawal(wallet, 40000, account_holder='کاربر تست')
        services.approve_withdrawal(request, admin_user=None)
        with self.captureOnCommitCallbacks(execute=True):
            services.mark_withdrawal_paid(request, admin_user=None)
        self.assertEqual(Notification.objects.filter(template_key='withdrawal_paid_customer').count(), 1)

    def test_reject_notifies_customer_with_reason(self):
        wallet = make_wallet(balance=100000)
        request = services.reserve_withdrawal(wallet, 40000, account_holder='کاربر تست')
        with self.captureOnCommitCallbacks(execute=True):
            services.reject_withdrawal(request, 'شبا نامعتبر بود', admin_user=None)
        notification = Notification.objects.get(template_key='withdrawal_rejected_customer')
        self.assertIn('شبا نامعتبر بود', notification.text)

    def test_customer_notifications_never_contain_bank_details(self):
        """ نکته‌ی امنیتی صریح: هیچ‌کدام از ۴ پیام مشتری نباید شماره کارت/شبای واقعی را در متن داشته باشند """
        wallet = make_wallet(balance=100000)
        card, iban = '6219861035427496', 'IR820540102680020817909002'
        with self.captureOnCommitCallbacks(execute=True):
            request = services.reserve_withdrawal(wallet, 40000, account_holder='کاربر تست', card_number=card, iban=iban)
        with self.captureOnCommitCallbacks(execute=True):
            services.approve_withdrawal(request, admin_user=None)
        with self.captureOnCommitCallbacks(execute=True):
            services.mark_withdrawal_paid(request, admin_user=None)

        customer_texts = Notification.objects.filter(
            template_key__in=[
                'withdrawal_requested_customer', 'withdrawal_approved_customer', 'withdrawal_paid_customer',
            ],
        ).values_list('text', flat=True)
        self.assertEqual(len(customer_texts), 3)
        for text in customer_texts:
            self.assertNotIn(card, text)
            self.assertNotIn(iban, text)
        # فقط پیامک مدیر مجاز است این‌ها را داشته باشد (خودش نیاز به واریز دستی دارد)
        admin_text = Notification.objects.get(template_key='withdrawal_requested_admin').text
        self.assertIn(card, admin_text)
        self.assertIn(iban, admin_text)


class WalletConcurrencyTests(TransactionTestCase):
    """
    هم‌زمانی واقعی (چند Thread، اتصال دیتابیس مجزا) روی select_for_update - دقیقاً همان الگوی
    accounts/tests_approval_concurrency.py. TestCase معمولی برای این منظور کافی نیست چون همه‌چیز
    داخل یک تراکنشِ رول‌بک‌شونده است و هیچ Threadی قفل واقعی نمی‌بیند.
    """
    serialized_rollback = True

    def run_threads(self, jobs):
        barrier = threading.Barrier(len(jobs))
        results = [None] * len(jobs)

        def worker(index, job):
            try:
                barrier.wait(timeout=30)
                results[index] = job()
            except BaseException as error:                                # noqa: BLE001
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

    def test_two_concurrent_debits_exactly_one_succeeds_without_overdraft(self):
        wallet = make_wallet(balance=100000)

        def make_job():
            def run():
                fresh = Wallet.objects.get(pk=wallet.pk)
                return services.debit_wallet(fresh, 80000, WalletTransaction.KIND_CART_PAYMENT)
            return run

        results = self.run_threads([make_job(), make_job()])

        successes = [r for r in results if isinstance(r, WalletTransaction)]
        failures = [r for r in results if isinstance(r, services.InsufficientBalanceError)]
        self.assertEqual(len(successes), 1, f'دقیقاً یکی باید موفق شود؛ نتایج: {results!r}')
        self.assertEqual(len(failures), 1, f'دقیقاً یکی باید InsufficientBalanceError بگیرد؛ نتایج: {results!r}')

        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, 20000)           # هرگز منفی، هرگز دوبار کسر نشده
        self.assertEqual(wallet.transactions.count(), 1)

    def test_two_concurrent_withdrawal_reservations_do_not_overreserve(self):
        wallet = make_wallet(balance=100000)

        def make_job():
            def run():
                fresh = Wallet.objects.get(pk=wallet.pk)
                return services.reserve_withdrawal(fresh, 70000, account_holder='کاربر تست')
            return run

        results = self.run_threads([make_job(), make_job()])

        successes = [r for r in results if isinstance(r, WithdrawalRequest)]
        failures = [r for r in results if isinstance(r, services.InsufficientBalanceError)]
        self.assertEqual(len(successes), 1, f'دقیقاً یکی باید موفق شود؛ نتایج: {results!r}')
        self.assertEqual(len(failures), 1, f'دقیقاً یکی باید InsufficientBalanceError بگیرد؛ نتایج: {results!r}')

        wallet.refresh_from_db()
        self.assertEqual(wallet.reserved_balance, 70000)   # نه ۱۴۰۰۰۰ (که از balance هم بیشتر می‌شد)
        self.assertGreaterEqual(wallet.balance, wallet.reserved_balance)
