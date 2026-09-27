"""
تست‌های رسیور order_canceled (Wallet Phase 4): بازگشت سهم کیف‌پول هنگام لغو سفارش، و
Idempotency در برابر سهمی که قبلاً (از مسیر دیگری) برگشته.
"""

import itertools
from decimal import Decimal
from unittest import mock

from django.test import TestCase

from accounts.models import CustomUser
from orders.models import Order
from wallet.models import Wallet, WalletTransaction

from . import checkout
from .models import Transaction

_seq = itertools.count(1)


def make_user_with_wallet(balance=0):
    user = CustomUser.objects.create_user(phone_number=f'0912005{next(_seq):04d}', first_name='کاربر تست')
    wallet = Wallet.objects.create(user=user, balance=balance)
    return user, wallet


def make_order(user, total_price):
    return Order.objects.create(
        user=user, first_name='کاربر', last_name='تست', phone=user.phone_number,
        address='تهران', payment_method='cash', shipping_cost=0, total_price=total_price,
    )


class OrderCancellationReversalTests(TestCase):
    def test_canceling_wallet_only_paid_order_reverses_wallet_share(self):
        user, wallet = make_user_with_wallet(balance=500000)
        order = make_order(user, total_price=500000)
        with mock.patch('holoo.receivers.confirm_payment_in_holoo'):
            with self.captureOnCommitCallbacks(execute=True):
                checkout.start_order_payment(order, user, Decimal('500000'))
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, 0)

        order.status = 'canceled'
        with self.captureOnCommitCallbacks(execute=True):
            order.save()

        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, 500000)
        txn = Transaction.objects.get(order=order)
        self.assertIsNotNone(txn.wallet_reversed_at)
        self.assertEqual(txn.status, 'success')   # پرداخت واقعاً اتفاق افتاده بود؛ status دست‌نخورده می‌ماند
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=wallet, kind=WalletTransaction.KIND_REVERSAL).count(), 1,
        )

    def test_canceling_order_with_no_wallet_share_does_nothing(self):
        user, wallet = make_user_with_wallet(balance=0)
        order = make_order(user, total_price=500000)
        Transaction.objects.create(user=user, order=order, amount=500000, authority='A' + 'x' * 10, status='success')

        order.status = 'canceled'
        with self.captureOnCommitCallbacks(execute=True):
            order.save()

        self.assertEqual(WalletTransaction.objects.filter(wallet=wallet).count(), 0)

    def test_canceling_order_whose_wallet_share_was_already_reversed_is_idempotent(self):
        """ سهمی که قبلاً (مثلاً از مسیر شکست درگاه) Reverse شده، دوباره Reverse نمی‌شود """
        user, wallet = make_user_with_wallet(balance=500000)
        order = make_order(user, total_price=500000)
        with mock.patch('holoo.receivers.confirm_payment_in_holoo'):
            with self.captureOnCommitCallbacks(execute=True):
                txn, _ = checkout.start_order_payment(order, user, Decimal('500000'))
        checkout.reverse_wallet_leg(txn.pk, reason='قبلاً به‌شکل دستی برگشته')
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, 500000)

        order.status = 'canceled'
        with self.captureOnCommitCallbacks(execute=True):
            order.save()

        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, 500000)   # دوباره برنگشته (وگرنه ۱۰۰۰۰۰۰ می‌شد)
        self.assertEqual(
            WalletTransaction.objects.filter(wallet=wallet, kind=WalletTransaction.KIND_REVERSAL).count(), 1,
        )

    def test_error_reversing_one_candidate_does_not_block_the_others(self):
        """ اگر یک ردیف خطا بدهد (مثلاً wallet_transaction گمشده)، بقیه‌ی سفارش‌ها/ردیف‌ها پردازش شوند """
        user, wallet = make_user_with_wallet(balance=500000)
        order = make_order(user, total_price=500000)
        # ردیف سالم
        with mock.patch('holoo.receivers.confirm_payment_in_holoo'):
            with self.captureOnCommitCallbacks(execute=True):
                healthy_txn, _ = checkout.start_order_payment(order, user, Decimal('300000'))
        # ردیف ناسازگار: wallet_amount>0 ولی wallet_transaction=None (دستی برای شبیه‌سازی داده‌ی خراب)
        broken_txn = Transaction.objects.create(
            user=user, order=order, amount=0, wallet_amount=200000,
            authority='WALLETBROKEN', status='success', ref_id='کیف پول',
        )

        order.status = 'canceled'
        with self.captureOnCommitCallbacks(execute=True):
            order.save()

        healthy_txn.refresh_from_db()
        broken_txn.refresh_from_db()
        self.assertIsNotNone(healthy_txn.wallet_reversed_at)   # سالم برگشته
        self.assertIsNone(broken_txn.wallet_reversed_at)       # خراب برگشته نشده، ولی حلقه نشکسته
