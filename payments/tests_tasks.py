"""
تست‌های تسک انقضای پرداخت ترکیبی معلق (Wallet Phase 4): انقضای واقعی بعد از آستانه، عدم
دست‌زدن به تراکنش‌های تازه/قبلاً‌پردازش‌شده، و Race با Callback واقعی کاربر.
"""

import itertools
import threading
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomUser
from orders.models import Order
from wallet.models import Wallet, WalletTransaction

from . import checkout, tasks
from .models import Transaction

_seq = itertools.count(1)


def make_user_with_wallet(balance=0):
    user = CustomUser.objects.create_user(phone_number=f'0912006{next(_seq):04d}', first_name='کاربر تست')
    wallet = Wallet.objects.create(user=user, balance=balance)
    return user, wallet


def make_order(user, total_price):
    return Order.objects.create(
        user=user, first_name='کاربر', last_name='تست', phone=user.phone_number,
        address='تهران', payment_method='cash', shipping_cost=0, total_price=total_price,
    )


class ExpireStalePendingWalletTransactionsTests(TestCase):
    def _start_mixed(self, user, order, wallet_amount):
        txn, _ = checkout.start_order_payment(order, user, Decimal(wallet_amount))
        return txn

    def test_stale_pending_transaction_is_expired_and_wallet_reversed(self):
        user, wallet = make_user_with_wallet(balance=200000)
        order = make_order(user, total_price=500000)
        txn = self._start_mixed(user, order, 200000)
        Transaction.objects.filter(pk=txn.pk).update(created_at=timezone.now() - timedelta(minutes=31))

        tasks.expire_single_pending_transaction(txn.pk)

        txn.refresh_from_db()
        wallet.refresh_from_db()
        self.assertEqual(txn.status, 'failed')
        self.assertIsNotNone(txn.wallet_reversed_at)
        self.assertEqual(wallet.balance, 200000)

    def test_fresh_pending_transaction_is_not_touched_by_finder(self):
        user, wallet = make_user_with_wallet(balance=200000)
        order = make_order(user, total_price=500000)
        txn = self._start_mixed(user, order, 200000)   # created_at = الان

        tasks.expire_stale_pending_wallet_transactions()

        txn.refresh_from_db()
        self.assertEqual(txn.status, 'pending')
        self.assertIsNone(txn.wallet_reversed_at)

    def test_already_resolved_transaction_is_left_alone(self):
        user, wallet = make_user_with_wallet(balance=200000)
        order = make_order(user, total_price=500000)
        txn = self._start_mixed(user, order, 200000)
        Transaction.objects.filter(pk=txn.pk).update(
            status='success', created_at=timezone.now() - timedelta(minutes=60),
        )

        result = tasks.expire_single_pending_transaction(txn.pk)

        self.assertEqual(result, "already processed")
        txn.refresh_from_db()
        self.assertEqual(txn.status, 'success')
        self.assertIsNone(txn.wallet_reversed_at)

    def test_finder_queues_only_stale_candidates(self):
        """
        finder فقط شناسه‌ها را می‌خواند و به .delay می‌سپارد (بدون قفل)؛ خودِ اجرای واقعی
        (تغییر status/Reverse) در تسک per-row است - همان الگوی تست‌های موجود پروژه برای
        زنجیره‌ی Celery (مثل payments.tests که .delay.call_count را چک می‌کند، نه اجرای واقعی
        تسک را، چون بدون Worker واقعی .delay صرفاً به صف Redis می‌رود و همین‌جا اجرا نمی‌شود)
        """
        user, wallet = make_user_with_wallet(balance=400000)
        order = make_order(user, total_price=500000)
        stale_txn = self._start_mixed(user, order, 200000)
        Transaction.objects.filter(pk=stale_txn.pk).update(created_at=timezone.now() - timedelta(minutes=31))

        order2 = make_order(user, total_price=200000)
        fresh_txn = self._start_mixed(user, order2, 100000)

        with mock.patch.object(tasks.expire_single_pending_transaction, 'delay') as delay:
            result = tasks.expire_stale_pending_wallet_transactions()

        self.assertEqual(result, "queued=1")
        delay.assert_called_once_with(stale_txn.pk)


class ExpiryRaceWithCallbackTests(TransactionTestCase):
    """
    هم‌زمانی واقعی: تسک انقضا و بازگشت واقعی کاربر از درگاه دقیقاً روی یک ردیف - قفل مشترک
    select_for_update باید تضمین کند فقط یکی از دو مسیر واقعاً Reverse می‌کند.
    """
    serialized_rollback = True

    def test_expiry_task_and_real_callback_race_exactly_one_reversal_happens(self):
        user, wallet = make_user_with_wallet(balance=200000)
        order = make_order(user, total_price=500000)
        txn, _ = checkout.start_order_payment(order, user, Decimal('200000'))
        Transaction.objects.filter(pk=txn.pk).update(created_at=timezone.now() - timedelta(minutes=31))

        barrier = threading.Barrier(2)
        results = [None, None]

        def run_expiry():
            try:
                barrier.wait(timeout=30)
                results[0] = tasks.expire_single_pending_transaction(txn.pk)
            except BaseException as error:                                # noqa: BLE001
                results[0] = error
            finally:
                connection.close()

        def run_callback():
            from django.test import Client
            client = Client()
            client.force_login(user)
            try:
                barrier.wait(timeout=30)
                results[1] = client.get(
                    f"{reverse('payments:callback')}?Authority={txn.authority}&Status=NOK",
                ).status_code
            except BaseException as error:                                # noqa: BLE001
                results[1] = error
            finally:
                connection.close()

        threads = [threading.Thread(target=run_expiry), threading.Thread(target=run_callback)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertFalse(any(t.is_alive() for t in threads), 'یک نخ گیر کرد (احتمال deadlock)')

        wallet.refresh_from_db()
        txn.refresh_from_db()
        self.assertEqual(txn.status, 'failed')
        self.assertIsNotNone(txn.wallet_reversed_at)
        self.assertEqual(wallet.balance, 200000)   # کاملاً برگشته - نه صفر (برگشت‌نخورده) نه ۴۰۰۰۰۰ (دوبار برگشته)
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=wallet, kind=WalletTransaction.KIND_REVERSAL).count(), 1,
        )
