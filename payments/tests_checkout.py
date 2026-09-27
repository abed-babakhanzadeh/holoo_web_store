"""
تست‌های هماهنگ‌کننده‌ی پرداخت ترکیبی (Wallet Phase 4): Wallet-only، Mixed، Gateway-only،
شکست/انصراف درگاه و بازگشت وجه، Duplicate Callback، و هم‌زمانی واقعی روی کیف‌پول.
"""

import itertools
import threading
from decimal import Decimal
from unittest import mock

from django.db import connection, transaction as db_transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from accounts.models import CustomUser
from notifications.models import Notification
from orders.models import Order
from wallet import services as wallet_services
from wallet.models import Wallet, WalletTransaction

from . import checkout
from .models import Transaction

_seq = itertools.count(1)


def make_user_with_wallet(balance=0):
    user = CustomUser.objects.create_user(phone_number=f'0912004{next(_seq):04d}', first_name='کاربر تست')
    wallet = Wallet.objects.create(user=user, balance=balance)
    return user, wallet


def make_order(user, total_price):
    return Order.objects.create(
        user=user, first_name='کاربر', last_name='تست', phone=user.phone_number,
        address='تهران', payment_method='cash', shipping_cost=0, total_price=total_price,
    )


class WalletOnlyPaymentTests(TestCase):
    def test_sufficient_balance_pays_full_order_without_gateway(self):
        user, wallet = make_user_with_wallet(balance=500000)
        order = make_order(user, total_price=500000)

        txn, redirect_kind = checkout.start_order_payment(order, user, Decimal('500000'))

        self.assertEqual(redirect_kind, 'result')
        self.assertEqual(txn.amount, 0)
        self.assertEqual(txn.wallet_amount, 500000)
        self.assertEqual(txn.status, 'success')
        self.assertEqual(txn.ref_id, 'کیف پول')
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, 0)
        self.assertTrue(order.is_paid)

    def test_insufficient_balance_raises_and_creates_nothing(self):
        user, wallet = make_user_with_wallet(balance=100000)
        order = make_order(user, total_price=500000)

        with self.assertRaises(wallet_services.InsufficientBalanceError):
            checkout.start_order_payment(order, user, Decimal('500000'))

        self.assertFalse(Transaction.objects.filter(order=order).exists())
        self.assertEqual(WalletTransaction.objects.filter(wallet=wallet).count(), 0)
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, 100000)

    def test_wallet_only_payment_fires_payment_succeeded_side_effects(self):
        user, wallet = make_user_with_wallet(balance=500000)
        order = make_order(user, total_price=500000)

        with mock.patch('holoo.receivers.confirm_payment_in_holoo') as holoo_task:
            with self.captureOnCommitCallbacks(execute=True):
                checkout.start_order_payment(order, user, Decimal('500000'))

        self.assertEqual(holoo_task.delay.call_count, 1)

    def test_wallet_only_customer_notification_does_not_render_none(self):
        """ ref_id='کیف پول' نه None - تا متن پیامک رشته‌ی لفظی «None» نداشته باشد """
        user, wallet = make_user_with_wallet(balance=500000)
        order = make_order(user, total_price=500000)

        with self.captureOnCommitCallbacks(execute=True):
            checkout.start_order_payment(order, user, Decimal('500000'))

        notification = Notification.objects.get(template_key='payment_succeeded_customer', recipient=user.phone_number)
        self.assertNotIn('None', notification.text)
        self.assertIn('کیف پول', notification.text)


class MixedPaymentTests(TestCase):
    def _start_mixed(self, user, order, wallet_amount):
        return checkout.start_order_payment(order, user, Decimal(wallet_amount))

    def _callback(self, txn, status='OK'):
        return self.client.get(f"{reverse('payments:callback')}?Authority={txn.authority}&Status={status}")

    def test_mixed_creates_gateway_leg_with_remaining_amount_only(self):
        user, wallet = make_user_with_wallet(balance=200000)
        order = make_order(user, total_price=500000)

        txn, redirect_kind = self._start_mixed(user, order, 200000)

        self.assertEqual(redirect_kind, 'gateway')
        self.assertEqual(txn.amount, 300000)
        self.assertEqual(txn.wallet_amount, 200000)
        self.assertEqual(txn.status, 'pending')
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, 0)   # سهم کیف‌پول همین الان و واقعاً کسر شده، نه صرفاً رزرو
        self.assertFalse(order.is_paid)       # هنوز درگاه تأیید نکرده

    def test_mixed_successful_gateway_callback_marks_order_paid(self):
        user, wallet = make_user_with_wallet(balance=200000)
        order = make_order(user, total_price=500000)
        txn, _ = self._start_mixed(user, order, 200000)
        self.client.force_login(user)

        with mock.patch('holoo.receivers.confirm_payment_in_holoo') as holoo_task:
            with self.captureOnCommitCallbacks(execute=True):
                self._callback(txn, status='OK')

        txn.refresh_from_db()
        self.assertEqual(txn.status, 'success')
        self.assertTrue(order.is_paid)
        self.assertEqual(holoo_task.delay.call_count, 1)

    def test_mixed_gateway_cancellation_reverses_wallet_leg(self):
        user, wallet = make_user_with_wallet(balance=200000)
        order = make_order(user, total_price=500000)
        txn, _ = self._start_mixed(user, order, 200000)
        self.client.force_login(user)

        self._callback(txn, status='NOK')

        txn.refresh_from_db()
        wallet.refresh_from_db()
        self.assertEqual(txn.status, 'failed')
        self.assertIsNotNone(txn.wallet_reversed_at)
        self.assertEqual(wallet.balance, 200000)   # کاملاً برگشته
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=wallet, kind=WalletTransaction.KIND_REVERSAL).count(), 1,
        )
        self.assertFalse(order.is_paid)

    def test_duplicate_failed_callback_reverses_wallet_leg_exactly_once(self):
        user, wallet = make_user_with_wallet(balance=200000)
        order = make_order(user, total_price=500000)
        txn, _ = self._start_mixed(user, order, 200000)
        self.client.force_login(user)

        self._callback(txn, status='NOK')
        self._callback(txn, status='NOK')
        self._callback(txn, status='OK')  # حتی تلاش برای برگرداندن به success نباید چیزی را عوض کند

        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, 200000)
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=wallet, kind=WalletTransaction.KIND_REVERSAL).count(), 1,
        )

    def test_amount_mismatch_in_mixed_payment_is_rejected_and_reverses_wallet(self):
        """ اگر مبلغ تأییدشده‌ی درگاه با remaining نخواند (نه با کل سفارش)، رد و سهم کیف‌پول برگردد """
        user, wallet = make_user_with_wallet(balance=200000)
        order = make_order(user, total_price=500000)
        txn, _ = self._start_mixed(user, order, 200000)
        Transaction.objects.filter(pk=txn.pk).update(amount=1)  # دستکاری مبلغ گیت‌وی
        self.client.force_login(user)

        self._callback(txn, status='OK')

        txn.refresh_from_db()
        wallet.refresh_from_db()
        self.assertEqual(txn.status, 'failed')
        self.assertIsNotNone(txn.wallet_reversed_at)
        self.assertEqual(wallet.balance, 200000)


class GatewayOnlyRegressionTests(TestCase):
    """ هر دو حالت wallet_amount=0 (مسیر جدید) هیچ رفتاری را نسبت به قبل از Phase 4 عوض نکند """

    def test_gateway_only_creates_transaction_with_zero_wallet_amount(self):
        user, wallet = make_user_with_wallet(balance=0)
        order = make_order(user, total_price=500000)

        txn, redirect_kind = checkout.start_order_payment(order, user, Decimal('0'))

        self.assertEqual(redirect_kind, 'gateway')
        self.assertEqual(txn.amount, 500000)
        self.assertEqual(txn.wallet_amount, 0)
        self.assertIsNone(txn.wallet_transaction)
        self.assertEqual(txn.status, 'pending')


class InvalidWalletAmountTests(TestCase):
    def test_negative_wallet_amount_rejected(self):
        user, wallet = make_user_with_wallet(balance=100000)
        order = make_order(user, total_price=500000)
        with self.assertRaises(checkout.InvalidWalletAmountError):
            checkout.start_order_payment(order, user, Decimal('-1'))

    def test_wallet_amount_above_order_total_rejected(self):
        user, wallet = make_user_with_wallet(balance=900000)
        order = make_order(user, total_price=500000)
        with self.assertRaises(checkout.InvalidWalletAmountError):
            checkout.start_order_payment(order, user, Decimal('600000'))


class WalletPaymentConcurrencyTests(TransactionTestCase):
    """
    هم‌زمانی واقعی (چند Thread، اتصال دیتابیس مجزا) - همان الگوی
    wallet.tests.WalletConcurrencyTests / accounts/tests_approval_concurrency.py.
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

    def test_two_concurrent_wallet_only_payments_on_same_order_exactly_one_succeeds(self):
        user, wallet = make_user_with_wallet(balance=500000)
        order = make_order(user, total_price=500000)

        def make_job():
            def run():
                fresh_order = Order.objects.get(pk=order.pk)
                return checkout.start_order_payment(fresh_order, user, Decimal('500000'))
            return run

        # TransactionTestCase واقعاً commit می‌کند، پس on_commit سمت برنده‌ی race واقعاً شلیک
        # می‌شود؛ confirm_payment_in_holoo را mock می‌کنیم تا این تست به دسترس‌پذیریِ Redis/Celery
        # واقعی وابسته نباشد (دقیقاً هم‌سبک بقیه‌ی تست‌های این فایل)
        with mock.patch('holoo.receivers.confirm_payment_in_holoo'):
            results = self.run_threads([make_job(), make_job()])

        successes = [r for r in results if isinstance(r, tuple)]
        failures = [r for r in results if isinstance(r, wallet_services.InsufficientBalanceError)]
        self.assertEqual(len(successes), 1, f'دقیقاً یکی باید موفق شود؛ نتایج: {results!r}')
        self.assertEqual(len(failures), 1, f'دقیقاً یکی باید InsufficientBalanceError بگیرد؛ نتایج: {results!r}')

        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, 0)   # هرگز منفی، هرگز دوبار کسر نشده
        self.assertEqual(Transaction.objects.filter(order=order).count(), 1)
