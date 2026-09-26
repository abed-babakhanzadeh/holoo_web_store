"""
تست‌های فاز ۲ کیف پول: داشبورد + جریان شارژ آزمایشی (Mock Top-up) از طریق ویو/URL واقعی.
برداشت بانکی عمداً در این فاز پیاده نشده (نگاه کنید wallet/tests.py برای تست‌های سرویس/مدل فاز ۱).
"""

import itertools

from django.test import TestCase
from django.urls import reverse

from accounts.models import CustomUser

from .models import Wallet, WalletTopupRequest, WalletTransaction

_seq = itertools.count(1)


def make_user():
    return CustomUser.objects.create_user(phone_number=f'0912004{next(_seq):04d}')


class WalletDashboardViewTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.wallet = Wallet.objects.create(user=self.user, balance=150000, reserved_balance=40000)

    def test_requires_login(self):
        response = self.client.get(reverse('wallet:dashboard'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response.url)

    def test_shows_balance_fields(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('wallet:dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['wallet'].balance, 150000)
        self.assertEqual(response.context['wallet'].reserved_balance, 40000)
        self.assertEqual(response.context['wallet'].available_balance, 110000)

    def test_creates_wallet_lazily_if_missing(self):
        other = make_user()
        self.client.force_login(other)
        response = self.client.get(reverse('wallet:dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Wallet.objects.filter(user=other).exists())
        self.assertEqual(response.context['wallet'].balance, 0)

    def test_kind_filter_deposit_and_withdraw(self):
        WalletTransaction.objects.create(wallet=self.wallet, amount=100000, kind=WalletTransaction.KIND_TOPUP, balance_after=100000)
        WalletTransaction.objects.create(wallet=self.wallet, amount=-30000, kind=WalletTransaction.KIND_WITHDRAWAL, balance_after=70000)
        self.client.force_login(self.user)

        response = self.client.get(reverse('wallet:dashboard'), {'kind': 'deposit'})
        self.assertEqual(len(response.context['transactions']), 1)
        self.assertGreater(response.context['transactions'][0].amount, 0)

        response = self.client.get(reverse('wallet:dashboard'), {'kind': 'withdraw'})
        self.assertEqual(len(response.context['transactions']), 1)
        self.assertLess(response.context['transactions'][0].amount, 0)

        response = self.client.get(reverse('wallet:dashboard'), {'kind': 'all'})
        self.assertEqual(len(response.context['transactions']), 2)

    def test_pagination(self):
        for i in range(15):
            WalletTransaction.objects.create(
                wallet=self.wallet, amount=1000, kind=WalletTransaction.KIND_TOPUP, balance_after=1000 * (i + 1),
            )
        self.client.force_login(self.user)
        response = self.client.get(reverse('wallet:dashboard'))
        self.assertEqual(response.context['page_obj'].paginator.num_pages, 2)
        self.assertEqual(len(response.context['transactions']), 10)


class WalletTopupViewTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def test_requires_login(self):
        response = self.client.get(reverse('wallet:topup'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response.url)

    def test_get_shows_form(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('wallet:topup'))
        self.assertEqual(response.status_code, 200)
        self.assertIn(50000, response.context['preset_amounts'])

    def test_post_valid_amount_creates_pending_request_and_redirects_to_gateway(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse('wallet:topup'), {'amount': '100000'})
        self.assertEqual(response.status_code, 302)
        request = WalletTopupRequest.objects.get(wallet__user=self.user)
        self.assertEqual(request.amount, 100000)
        self.assertEqual(request.status, WalletTopupRequest.STATUS_PENDING)
        self.assertIn(request.authority, response.url)

    def test_post_amount_below_minimum_is_rejected(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse('wallet:topup'), {'amount': '10000'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('error', response.context)
        self.assertFalse(WalletTopupRequest.objects.exists())

    def test_post_non_numeric_amount_is_rejected(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse('wallet:topup'), {'amount': 'abc'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('error', response.context)
        self.assertFalse(WalletTopupRequest.objects.exists())


class WalletMockGatewayViewTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.wallet = Wallet.objects.create(user=self.user)
        self.topup = WalletTopupRequest.objects.create(wallet=self.wallet, amount=100000, authority='WLT-TEST-1')

    def test_requires_login(self):
        response = self.client.get(reverse('wallet:mock_gateway', args=[self.topup.authority]))
        self.assertEqual(response.status_code, 302)

    def test_shows_pending_request_of_the_owner(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('wallet:mock_gateway', args=[self.topup.authority]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['topup'], self.topup)

    def test_other_users_request_is_not_visible(self):
        other = make_user()
        self.client.force_login(other)
        response = self.client.get(reverse('wallet:mock_gateway', args=[self.topup.authority]))
        self.assertEqual(response.status_code, 404)

    def test_already_processed_request_is_not_shown_again(self):
        # وضعیت success بدون transaction لینک‌شده مجاز نیست (constraint دیتابیس)
        txn = WalletTransaction.objects.create(
            wallet=self.wallet, amount=self.topup.amount, kind=WalletTransaction.KIND_TOPUP,
            balance_after=self.topup.amount,
        )
        self.topup.status = WalletTopupRequest.STATUS_SUCCESS
        self.topup.transaction = txn
        self.topup.save(update_fields=['status', 'transaction'])
        self.client.force_login(self.user)
        response = self.client.get(reverse('wallet:mock_gateway', args=[self.topup.authority]))
        self.assertEqual(response.status_code, 404)


class WalletTopupCallbackViewTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.wallet = Wallet.objects.create(user=self.user, balance=0)
        self.topup = WalletTopupRequest.objects.create(wallet=self.wallet, amount=100000, authority='WLT-TEST-2')

    def _callback(self, status):
        return self.client.get(f"{reverse('wallet:topup_callback')}?authority={self.topup.authority}&status={status}")

    def test_requires_login(self):
        response = self._callback('OK')
        self.assertEqual(response.status_code, 302)

    def test_successful_callback_credits_wallet_and_creates_ledger_entry(self):
        self.client.force_login(self.user)
        response = self._callback('OK')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['success'])
        # این‌جا عمداً از context رندرشده چک می‌شود (نه فقط دوباره‌خوانی مستقل از DB)، چون یک باگ
        # واقعی همین‌جا پیدا شد: locked.wallet (نمونه‌ی گرفته‌شده قبل از credit_wallet) به‌روز
        # نمی‌شد، چون credit_wallet نمونه‌ی دیگری از Wallet را قفل/ذخیره می‌کند - نتیجه‌اش نمایش
        # موجودی کهنه (صفر) در صفحه‌ی نتیجه بود با این‌که در دیتابیس درست شارژ شده بود.
        self.assertEqual(response.context['wallet'].balance, 100000)
        self.assertContains(response, '100000')

        self.wallet.refresh_from_db()
        self.topup.refresh_from_db()
        self.assertEqual(self.wallet.balance, 100000)
        self.assertEqual(self.topup.status, WalletTopupRequest.STATUS_SUCCESS)
        self.assertIsNotNone(self.topup.transaction)
        self.assertEqual(self.topup.transaction.amount, 100000)
        self.assertEqual(self.topup.transaction.kind, WalletTransaction.KIND_TOPUP)
        self.assertEqual(self.topup.transaction.reference_type, 'wallet_topup_request')
        self.assertEqual(self.topup.transaction.reference_id, self.topup.pk)

    def test_cancelled_callback_does_not_change_balance(self):
        self.client.force_login(self.user)
        response = self._callback('CANCEL')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['success'])

        self.wallet.refresh_from_db()
        self.topup.refresh_from_db()
        self.assertEqual(self.wallet.balance, 0)
        self.assertEqual(self.topup.status, WalletTopupRequest.STATUS_FAILED)
        self.assertIsNone(self.topup.transaction)

    def test_double_submit_does_not_credit_twice(self):
        """ رفرش صفحه‌ی بازگشت از درگاه نباید کیف پول را دوبار شارژ کند """
        self.client.force_login(self.user)
        self._callback('OK')
        self._callback('OK')

        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, 100000)
        self.assertEqual(WalletTransaction.objects.filter(wallet=self.wallet).count(), 1)

    def test_other_users_request_is_not_accessible(self):
        other = make_user()
        self.client.force_login(other)
        response = self._callback('OK')
        self.assertEqual(response.status_code, 404)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, 0)
