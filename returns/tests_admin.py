"""
تست‌های Part B.2: اکشن‌های ادمین ReturnRequestAdmin (تأیید/دریافت/صف‌بازپرداخت/رد/تکمیل)
با رعایت مجوزها - دقیقاً هم‌الگوی accounts/tests_approval_admin.py.
"""

from django.contrib.admin import helpers
from django.test import TestCase
from django.urls import reverse

from accounts.models import CustomUser, UserBankAccount
from returns.models import ReturnRequest
from returns.services import approve_return_request, create_return_request, mark_items_received, mark_refund_pending
from returns.tests import ReturnsTestMixin
from wallet.models import Wallet, WalletTransaction


class ReturnRequestAdminActionsTests(ReturnsTestMixin, TestCase):
    def setUp(self):
        self.customer = self.make_user()
        self.category = self.make_category()
        self.product = self.make_product(self.category)
        self.reason = self.make_reason()
        self.superuser = CustomUser.objects.create_superuser(phone_number='09150001001')
        self.client.force_login(self.superuser)
        self.changelist_url = reverse('admin:returns_returnrequest_changelist')

    def make_request(self, quantity=1, order_quantity=None):
        order = self.make_order(self.customer)
        item = self.make_order_item(order, self.product, price=50000, quantity=order_quantity or quantity)
        return create_return_request(
            order, self.customer,
            [{'order_item': item, 'reason': self.reason, 'requested_quantity': quantity}],
            refund_method=ReturnRequest.REFUND_WALLET,
        )

    def _action_post(self, action, pks, **extra):
        data = {'action': action, helpers.ACTION_CHECKBOX_NAME: [str(pk) for pk in pks], **extra}
        return self.client.post(self.changelist_url, data, follow=False)

    # ----- مجوزها -----

    def test_add_permission_is_disabled(self):
        response = self.client.get(reverse('admin:returns_returnrequest_add'))
        self.assertEqual(response.status_code, 403)

    def test_delete_permission_is_disabled(self):
        request = self.make_request()
        response = self.client.post(self.changelist_url, {
            'action': 'delete_selected', helpers.ACTION_CHECKBOX_NAME: [str(request.pk)], 'post': 'yes',
        })
        # 'delete_selected' اصلاً در فهرست اکشن‌های این ادمین نیست (has_delete_permission=False)؛
        # جنگو یا فرم را با خطا دوباره نشان می‌دهد (۲۰۰) یا به changelist برمی‌گردد (۳۰۲) - در هر
        # دو حالت مهم این است که چیزی حذف نشود.
        self.assertIn(response.status_code, (200, 302))
        self.assertTrue(ReturnRequest.objects.filter(pk=request.pk).exists())

    def test_non_staff_cannot_access_changelist_or_actions(self):
        self.client.logout()
        plain_user = CustomUser.objects.create_user(phone_number='09150001002')
        self.client.force_login(plain_user)
        request = self.make_request()

        self.assertNotEqual(self.client.get(self.changelist_url).status_code, 200)
        response = self._action_post('approve_selected_action', [request.pk])
        self.assertNotEqual(response.status_code, 200)
        request.refresh_from_db()
        self.assertEqual(request.status, ReturnRequest.STATUS_PENDING)   # هیچ اثری نگذاشته باشد

    def test_changelist_and_change_page_render(self):
        request = self.make_request()
        self.assertEqual(self.client.get(self.changelist_url).status_code, 200)
        change_url = reverse('admin:returns_returnrequest_change', args=[request.pk])
        self.assertEqual(self.client.get(change_url).status_code, 200)

    # ----- approve_selected_action -----

    def test_approve_action_transitions_pending_to_approved(self):
        request = self.make_request()
        self._action_post('approve_selected_action', [request.pk])
        request.refresh_from_db()
        self.assertEqual(request.status, ReturnRequest.STATUS_APPROVED)
        self.assertEqual(request.decided_by_id, self.superuser.pk)

    def test_approve_action_on_wrong_state_reports_error_without_crashing(self):
        request = self.make_request()
        approve_return_request(request, self.superuser)
        response = self._action_post('approve_selected_action', [request.pk])
        self.assertEqual(response.status_code, 302)   # به changelist برمی‌گردد، نه خطای ۵۰۰
        request.refresh_from_db()
        self.assertEqual(request.status, ReturnRequest.STATUS_APPROVED)   # دست‌نخورده مانده

    def test_approve_action_supports_bulk_selection(self):
        r1, r2 = self.make_request(), self.make_request()
        self._action_post('approve_selected_action', [r1.pk, r2.pk])
        r1.refresh_from_db()
        r2.refresh_from_db()
        self.assertEqual((r1.status, r2.status), (ReturnRequest.STATUS_APPROVED, ReturnRequest.STATUS_APPROVED))

    # ----- mark_items_received_action -----

    def test_mark_items_received_requires_exactly_one_selected_row(self):
        r1, r2 = self.make_request(), self.make_request()
        approve_return_request(r1, self.superuser)
        approve_return_request(r2, self.superuser)
        response = self._action_post('mark_items_received_action', [r1.pk, r2.pk])
        self.assertEqual(response.status_code, 302)
        r1.refresh_from_db()
        self.assertEqual(r1.status, ReturnRequest.STATUS_APPROVED)   # هیچ‌کدام عوض نشده باشند

    def test_mark_items_received_shows_intermediate_form_then_applies(self):
        request = self.make_request(quantity=3, order_quantity=3)
        approve_return_request(request, self.superuser)
        item = request.items.get()

        shown = self._action_post('mark_items_received_action', [request.pk])
        self.assertEqual(shown.status_code, 200)
        self.assertContains(shown, f'item_{item.pk}')

        applied = self._action_post(
            'mark_items_received_action', [request.pk], apply='1', **{f'item_{item.pk}': '2'},
        )
        self.assertEqual(applied.status_code, 302)
        request.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(request.status, ReturnRequest.STATUS_ITEM_RECEIVED)
        self.assertEqual(item.approved_quantity, 2)

    def test_mark_items_received_out_of_range_quantity_is_rejected_by_form(self):
        request = self.make_request(quantity=2, order_quantity=2)
        approve_return_request(request, self.superuser)
        item = request.items.get()

        response = self._action_post(
            'mark_items_received_action', [request.pk], apply='1', **{f'item_{item.pk}': '5'},
        )
        self.assertEqual(response.status_code, 200)   # فرم دوباره با خطا نشان داده می‌شود
        request.refresh_from_db()
        self.assertEqual(request.status, ReturnRequest.STATUS_APPROVED)

    # ----- mark_refund_pending_action -----

    def test_mark_refund_pending_action_computes_amounts(self):
        request = self.make_request(quantity=2, order_quantity=2)
        approve_return_request(request, self.superuser)
        item = request.items.get()
        mark_items_received(request, {item.pk: 2}, self.superuser)

        self._action_post('mark_refund_pending_action', [request.pk])
        request.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(request.status, ReturnRequest.STATUS_REFUND_PENDING)
        self.assertEqual(item.refund_amount, 100000)

    # ----- reject_selected_action -----

    def test_reject_action_requires_exactly_one_selected_row(self):
        r1, r2 = self.make_request(), self.make_request()
        response = self._action_post('reject_selected_action', [r1.pk, r2.pk])
        self.assertEqual(response.status_code, 302)
        r1.refresh_from_db()
        self.assertEqual(r1.status, ReturnRequest.STATUS_PENDING)

    def test_reject_action_shows_intermediate_form_then_applies_with_reason(self):
        request = self.make_request()

        shown = self._action_post('reject_selected_action', [request.pk])
        self.assertEqual(shown.status_code, 200)
        self.assertContains(shown, 'reason')

        applied = self._action_post('reject_selected_action', [request.pk], apply='1', reason='کالا استفاده‌شده بود')
        self.assertEqual(applied.status_code, 302)
        request.refresh_from_db()
        self.assertEqual(request.status, ReturnRequest.STATUS_REJECTED)
        self.assertEqual(request.rejection_reason, 'کالا استفاده‌شده بود')

    def test_reject_action_without_reason_is_rejected_by_form(self):
        request = self.make_request()
        response = self._action_post('reject_selected_action', [request.pk], apply='1', reason='')
        self.assertEqual(response.status_code, 200)
        request.refresh_from_db()
        self.assertEqual(request.status, ReturnRequest.STATUS_PENDING)

    # ----- complete_refund_action -----

    def test_complete_refund_action_wallet_method_credits_wallet(self):
        request = self.make_request(quantity=2, order_quantity=2)
        approve_return_request(request, self.superuser)
        item = request.items.get()
        mark_items_received(request, {item.pk: 2}, self.superuser)
        mark_refund_pending(request)

        self._action_post('complete_refund_action', [request.pk])
        request.refresh_from_db()
        self.assertEqual(request.status, ReturnRequest.STATUS_COMPLETED)
        self.assertIsNotNone(request.wallet_transaction_id)
        self.assertEqual(request.wallet_transaction.kind, WalletTransaction.KIND_REFUND)
        self.assertEqual(Wallet.objects.get(user=self.customer).balance, 100000)

    def test_complete_refund_action_on_wrong_state_reports_error(self):
        request = self.make_request()
        response = self._action_post('complete_refund_action', [request.pk])
        self.assertEqual(response.status_code, 302)
        request.refresh_from_db()
        self.assertEqual(request.status, ReturnRequest.STATUS_PENDING)

    def test_complete_refund_action_bank_method_does_not_touch_wallet(self):
        account = UserBankAccount.objects.create(
            user=self.customer, account_holder_first_name='علی', account_holder_last_name='رضایی',
            card_number='6037991234567890',
        )
        order = self.make_order(self.customer)
        item = self.make_order_item(order, self.product, price=50000, quantity=1)
        request = create_return_request(
            order, self.customer, [{'order_item': item, 'reason': self.reason, 'requested_quantity': 1}],
            refund_method=ReturnRequest.REFUND_BANK, bank_account=account,
        )
        approve_return_request(request, self.superuser)
        return_item = request.items.get()
        mark_items_received(request, {return_item.pk: 1}, self.superuser)
        mark_refund_pending(request)

        self._action_post('complete_refund_action', [request.pk])
        request.refresh_from_db()
        self.assertEqual(request.status, ReturnRequest.STATUS_COMPLETED)
        self.assertIsNone(request.wallet_transaction_id)
        self.assertFalse(Wallet.objects.filter(user=self.customer).exists())
