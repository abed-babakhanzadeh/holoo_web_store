"""
تست‌های چرخه‌ی درخواست برداشت: فرم/ویو سمت کاربر + اقدامات ادمین (تأیید/رد).
منطق مالی خودش (reserve/complete/reject_withdrawal) در wallet/tests.py تست شده؛ اینجا فقط
مسیر واقعی HTTP (فرم کاربر + دکمه‌های ادمین) روی همان توابع سرویس سنجیده می‌شود.
"""

import itertools

from django.test import TestCase
from django.urls import reverse

from accounts.models import CustomUser

from .forms import WithdrawalRequestForm
from .models import Wallet, WithdrawalRequest

_seq = itertools.count(1)


def make_user():
    return CustomUser.objects.create_user(phone_number=f'0912005{next(_seq):04d}')


class WithdrawalRequestFormTests(TestCase):
    def setUp(self):
        self.wallet = Wallet.objects.create(user=make_user(), balance=100000)

    def valid_data(self, **overrides):
        data = {
            'amount': '50000', 'iban': 'IR820540102680020817909002',
            'card_number': '6219-8610-3542-7496', 'account_holder': 'کاربر تست',
        }
        data.update(overrides)
        return data

    def test_valid_data_is_accepted_and_normalized(self):
        form = WithdrawalRequestForm(self.valid_data(), wallet=self.wallet)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['amount'], 50000)
        self.assertEqual(form.cleaned_data['iban'], 'IR820540102680020817909002')
        self.assertEqual(form.cleaned_data['card_number'], '6219861035427496')

    def test_iban_without_ir_prefix_is_normalized(self):
        form = WithdrawalRequestForm(self.valid_data(iban='820540102680020817909002'), wallet=self.wallet)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['iban'], 'IR820540102680020817909002')

    def test_amount_above_available_balance_is_rejected(self):
        form = WithdrawalRequestForm(self.valid_data(amount='200000'), wallet=self.wallet)
        self.assertFalse(form.is_valid())
        self.assertIn('amount', form.errors)

    def test_amount_above_available_balance_accounts_for_reserved(self):
        self.wallet.reserved_balance = 80000
        self.wallet.save(update_fields=['reserved_balance'])
        form = WithdrawalRequestForm(self.valid_data(amount='30000'), wallet=self.wallet)
        self.assertFalse(form.is_valid())  # available = 100000-80000 = 20000 < 30000

    def test_invalid_iban_is_rejected(self):
        for bad_iban in ('123', 'IR12', 'ABCDEF', ''):
            with self.subTest(iban=bad_iban):
                form = WithdrawalRequestForm(self.valid_data(iban=bad_iban), wallet=self.wallet)
                self.assertFalse(form.is_valid())
                self.assertIn('iban', form.errors)

    def test_invalid_card_number_is_rejected(self):
        for bad_card in ('123', '12345678901234567', 'abcd-efgh-ijkl-mnop', ''):
            with self.subTest(card=bad_card):
                form = WithdrawalRequestForm(self.valid_data(card_number=bad_card), wallet=self.wallet)
                self.assertFalse(form.is_valid())
                self.assertIn('card_number', form.errors)

    def test_missing_account_holder_is_rejected(self):
        form = WithdrawalRequestForm(self.valid_data(account_holder=''), wallet=self.wallet)
        self.assertFalse(form.is_valid())
        self.assertIn('account_holder', form.errors)


class WalletWithdrawViewTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.wallet = Wallet.objects.create(user=self.user, balance=100000)

    def valid_post_data(self, **overrides):
        data = {
            'amount': '50000', 'iban': 'IR820540102680020817909002',
            'card_number': '6219861035427496', 'account_holder': 'کاربر تست',
        }
        data.update(overrides)
        return data

    def test_requires_login(self):
        response = self.client.get(reverse('wallet:withdraw'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response.url)

    def test_get_shows_form_and_history(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('wallet:withdraw'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['wallet'].available_balance, 100000)

    def test_valid_post_reserves_amount_without_touching_balance(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse('wallet:withdraw'), self.valid_post_data())
        self.assertEqual(response.status_code, 302)

        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, 100000)          # دست‌نخورده
        self.assertEqual(self.wallet.reserved_balance, 50000)
        self.assertEqual(self.wallet.available_balance, 50000)

        request = WithdrawalRequest.objects.get(wallet=self.wallet)
        self.assertEqual(request.status, WithdrawalRequest.STATUS_PENDING)
        self.assertEqual(request.card_number_snapshot, '6219861035427496')
        self.assertEqual(request.iban_snapshot, 'IR820540102680020817909002')

    def test_amount_above_balance_shows_form_error_and_creates_nothing(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse('wallet:withdraw'), self.valid_post_data(amount='999999'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('amount', response.context['form'].errors)
        self.assertFalse(WithdrawalRequest.objects.exists())
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.reserved_balance, 0)

    def test_invalid_iban_shows_form_error_and_creates_nothing(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse('wallet:withdraw'), self.valid_post_data(iban='bad'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('iban', response.context['form'].errors)
        self.assertFalse(WithdrawalRequest.objects.exists())

    def test_history_shows_own_requests_with_status(self):
        WithdrawalRequest.objects.create(wallet=self.wallet, amount=10000, account_holder_snapshot='کاربر تست')
        other_wallet = Wallet.objects.create(user=make_user(), balance=0)
        WithdrawalRequest.objects.create(wallet=other_wallet, amount=20000, account_holder_snapshot='کاربر دیگر')

        self.client.force_login(self.user)
        response = self.client.get(reverse('wallet:withdraw'))
        requests = list(response.context['withdrawal_requests'])
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].wallet, self.wallet)


class WithdrawalAdminActionTests(TestCase):
    def setUp(self):
        self.admin = CustomUser.objects.create_superuser(phone_number=f'0912006{next(_seq):04d}')
        self.wallet = Wallet.objects.create(user=make_user(), balance=100000, reserved_balance=40000)
        self.request = WithdrawalRequest.objects.create(
            wallet=self.wallet, amount=40000, account_holder_snapshot='کاربر تست',
            card_number_snapshot='6219861035427496', iban_snapshot='IR820540102680020817909002',
        )
        self.client.force_login(self.admin)

    def test_changelist_shows_expected_columns(self):
        response = self.client.get(reverse('admin:wallet_withdrawalrequest_changelist'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '6219861035427496')
        self.assertContains(response, 'IR820540102680020817909002')
        self.assertContains(response, 'کاربر تست')

    def test_status_filter_is_available(self):
        response = self.client.get(reverse('admin:wallet_withdrawalrequest_changelist'), {'status': 'PENDING'})
        self.assertEqual(response.status_code, 200)

    def test_approve_action_transitions_to_approved_without_touching_balance(self):
        response = self.client.get(reverse('admin:wallet_withdrawalrequest_approve', args=[self.request.pk]))
        self.assertEqual(response.status_code, 302)

        self.wallet.refresh_from_db()
        self.request.refresh_from_db()
        self.assertEqual(self.wallet.balance, 100000)           # هنوز کسر نشده
        self.assertEqual(self.wallet.reserved_balance, 40000)   # هنوز بلوکه
        self.assertEqual(self.request.status, WithdrawalRequest.STATUS_APPROVED)
        self.assertIsNone(self.request.transaction)
        self.assertEqual(self.request.decided_by, self.admin)

    def test_approve_twice_is_a_structural_no_op(self):
        self.client.get(reverse('admin:wallet_withdrawalrequest_approve', args=[self.request.pk]))
        response = self.client.get(reverse('admin:wallet_withdrawalrequest_approve', args=[self.request.pk]))
        self.assertEqual(response.status_code, 302)  # پیام خطا با redirect، نه کرش
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, WithdrawalRequest.STATUS_APPROVED)  # همان حالت، تغییر مضاعف نکرد

    def test_mark_paid_requires_prior_approval(self):
        """ درخواستِ هنوز PENDING نباید بتواند مستقیم «پرداخت شد» بخورد """
        response = self.client.get(reverse('admin:wallet_withdrawalrequest_mark_paid', args=[self.request.pk]))
        self.assertEqual(response.status_code, 302)  # پیام خطا، نه کرش
        self.wallet.refresh_from_db()
        self.request.refresh_from_db()
        self.assertEqual(self.wallet.balance, 100000)
        self.assertEqual(self.request.status, WithdrawalRequest.STATUS_PENDING)

    def test_mark_paid_action_completes_withdrawal_via_services(self):
        self.client.get(reverse('admin:wallet_withdrawalrequest_approve', args=[self.request.pk]))
        response = self.client.get(reverse('admin:wallet_withdrawalrequest_mark_paid', args=[self.request.pk]))
        self.assertEqual(response.status_code, 302)

        self.wallet.refresh_from_db()
        self.request.refresh_from_db()
        self.assertEqual(self.wallet.balance, 60000)            # 100000 - 40000
        self.assertEqual(self.wallet.reserved_balance, 0)
        self.assertEqual(self.request.status, WithdrawalRequest.STATUS_COMPLETED)
        self.assertIsNotNone(self.request.transaction)
        self.assertIsNotNone(self.request.paid_at)

    def test_mark_paid_twice_is_a_structural_no_op(self):
        self.client.get(reverse('admin:wallet_withdrawalrequest_approve', args=[self.request.pk]))
        self.client.get(reverse('admin:wallet_withdrawalrequest_mark_paid', args=[self.request.pk]))
        self.wallet.refresh_from_db()
        balance_after_first = self.wallet.balance

        response = self.client.get(reverse('admin:wallet_withdrawalrequest_mark_paid', args=[self.request.pk]))
        self.assertEqual(response.status_code, 302)

        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, balance_after_first)  # دوباره کسر نشد

    def test_reject_action_requires_reason(self):
        response = self.client.post(reverse('admin:wallet_withdrawalrequest_reject', args=[self.request.pk]), {'reason': ''})
        self.assertEqual(response.status_code, 200)  # فرم دوباره با خطا نشان داده می‌شود
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, WithdrawalRequest.STATUS_PENDING)

    def test_reject_action_releases_reserved_and_saves_reason(self):
        response = self.client.post(
            reverse('admin:wallet_withdrawalrequest_reject', args=[self.request.pk]), {'reason': 'شماره شبا نامعتبر بود'},
        )
        self.assertEqual(response.status_code, 302)

        self.wallet.refresh_from_db()
        self.request.refresh_from_db()
        self.assertEqual(self.wallet.balance, 100000)           # دست‌نخورده
        self.assertEqual(self.wallet.reserved_balance, 0)       # آزاد شد
        self.assertEqual(self.request.status, WithdrawalRequest.STATUS_REJECTED)
        self.assertEqual(self.request.rejection_reason, 'شماره شبا نامعتبر بود')

    def test_reject_action_from_approved_also_releases_reserved(self):
        self.client.get(reverse('admin:wallet_withdrawalrequest_approve', args=[self.request.pk]))
        response = self.client.post(
            reverse('admin:wallet_withdrawalrequest_reject', args=[self.request.pk]), {'reason': 'شبا نادرست بود'},
        )
        self.assertEqual(response.status_code, 302)
        self.wallet.refresh_from_db()
        self.request.refresh_from_db()
        self.assertEqual(self.wallet.balance, 100000)
        self.assertEqual(self.wallet.reserved_balance, 0)
        self.assertEqual(self.request.status, WithdrawalRequest.STATUS_REJECTED)

    def test_reject_already_completed_request_is_rejected_structurally(self):
        self.client.get(reverse('admin:wallet_withdrawalrequest_approve', args=[self.request.pk]))
        self.client.get(reverse('admin:wallet_withdrawalrequest_mark_paid', args=[self.request.pk]))
        response = self.client.post(
            reverse('admin:wallet_withdrawalrequest_reject', args=[self.request.pk]), {'reason': 'تلاش دیرهنگام'},
        )
        self.assertEqual(response.status_code, 302)
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, WithdrawalRequest.STATUS_COMPLETED)  # تغییر نکرد

    def test_non_staff_cannot_access_admin_actions(self):
        self.client.logout()
        self.client.force_login(self.wallet.user)
        response = self.client.get(reverse('admin:wallet_withdrawalrequest_approve', args=[self.request.pk]))
        self.assertNotEqual(response.status_code, 200)
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, WithdrawalRequest.STATUS_PENDING)
