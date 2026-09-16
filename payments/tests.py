"""تست مسیر بازگشت از درگاه — تمرکز روی idempotency و مالکیت تراکنش."""

from unittest import mock

from django.test import TestCase
from django.urls import reverse

from accounts.models import CustomUser
from orders.models import Order
from payments.models import Transaction


class PaymentCallbackTests(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(phone_number='09120000010', first_name='علی')
        self.other = CustomUser.objects.create_user(phone_number='09120000011')
        self.order = Order.objects.create(
            user=self.user, first_name='علی', last_name='رضایی', phone='09120000010',
            address='تهران', payment_method='cash', shipping_cost=200000, total_price=500000,
        )
        self.client.force_login(self.user)

    def _start_payment(self):
        self.client.get(reverse('payments:start_payment', args=[self.order.id]))
        return Transaction.objects.get(order=self.order)

    def _callback(self, txn, status='OK'):
        return self.client.get(f"{reverse('payments:callback')}?Authority={txn.authority}&Status={status}")

    # --- شروع پرداخت ---

    def test_repeated_start_reuses_pending_transaction(self):
        first = self._start_payment()
        self.client.get(reverse('payments:start_payment', args=[self.order.id]))
        self.assertEqual(Transaction.objects.filter(order=self.order).count(), 1)
        self.assertEqual(Transaction.objects.get(order=self.order).authority, first.authority)

    def test_paid_order_cannot_start_payment_again(self):
        txn = self._start_payment()
        self._callback(txn)
        response = self.client.get(reverse('payments:start_payment', args=[self.order.id]))
        self.assertRedirects(response, reverse('orders:order_history'), fetch_redirect_response=False)

    # --- idempotency ---

    def test_successful_callback_marks_transaction_and_fires_side_effects(self):
        txn = self._start_payment()
        # ترتیب مهم است: captureOnCommitCallbacks کال‌بک‌ها را هنگام *خروج* از بلاک خودش
        # اجرا می‌کند، پس mock باید بیرونی‌تر باشد وگرنه تا آن لحظه برداشته شده است
        with mock.patch('holoo.receivers.confirm_payment_in_holoo') as holoo_task:
            with self.captureOnCommitCallbacks(execute=True):
                self._callback(txn)
        txn.refresh_from_db()

        self.assertEqual(txn.status, 'success')
        self.assertTrue(txn.ref_id)
        self.assertEqual(holoo_task.delay.call_count, 1)

    def test_refreshing_callback_does_not_repeat_side_effects(self):
        """ رفرش صفحه‌ی بازگشت نباید سند دریافت وجه تکراری در حسابداری بسازد """
        txn = self._start_payment()
        with mock.patch('holoo.receivers.confirm_payment_in_holoo'):
            with self.captureOnCommitCallbacks(execute=True):
                self._callback(txn)

        with mock.patch('holoo.receivers.confirm_payment_in_holoo') as holoo_task:
            with mock.patch('notifications.receivers.notify') as notify:
                with self.captureOnCommitCallbacks(execute=True):
                    self._callback(txn)
                    self._callback(txn)

        self.assertEqual(holoo_task.delay.call_count, 0)
        self.assertEqual(notify.call_count, 0)
        self.assertEqual(Transaction.objects.filter(order=self.order, status='success').count(), 1)

    def test_failed_callback_marks_transaction_failed(self):
        txn = self._start_payment()
        with mock.patch('holoo.receivers.confirm_payment_in_holoo') as holoo_task:
            self._callback(txn, status='NOK')
        txn.refresh_from_db()

        self.assertEqual(txn.status, 'failed')
        self.assertEqual(holoo_task.delay.call_count, 0)

    def test_failed_transaction_cannot_be_flipped_to_success(self):
        txn = self._start_payment()
        self._callback(txn, status='NOK')
        self._callback(txn, status='OK')
        txn.refresh_from_db()
        self.assertEqual(txn.status, 'failed')

    # --- مالکیت و اعتبار ---

    def test_other_user_cannot_trigger_callback(self):
        txn = self._start_payment()
        self.client.force_login(self.other)
        self.assertEqual(self._callback(txn).status_code, 404)

    def test_other_user_cannot_open_gateway_page(self):
        txn = self._start_payment()
        self.client.force_login(self.other)
        response = self.client.get(reverse('payments:mock_gateway', args=[txn.authority]))
        self.assertEqual(response.status_code, 404)

    def test_callback_without_authority_is_404(self):
        self.assertEqual(self.client.get(reverse('payments:callback')).status_code, 404)

    def test_amount_mismatch_is_rejected(self):
        """ اگر مبلغ تراکنش با مبلغ سفارش نخواند، نباید بی‌سروصدا موفق ثبت شود """
        txn = self._start_payment()
        Transaction.objects.filter(pk=txn.pk).update(amount=1)
        with mock.patch('holoo.receivers.confirm_payment_in_holoo') as holoo_task:
            self._callback(txn)
        txn.refresh_from_db()

        self.assertEqual(txn.status, 'failed')
        self.assertEqual(holoo_task.delay.call_count, 0)
