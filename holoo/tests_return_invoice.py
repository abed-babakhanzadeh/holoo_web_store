"""
ثبت فاکتور «برگشت از فروش» مرجوعی در هلو (holoo/invoice.py::build_return_payload، wire، کلاینت، send_return_to_holoo و شنونده‌ها).
هیچ تماسی به هلو نیست؛ اجرای واقعی روی Holoo2 جداست (ثبت، یافتن با فهرست نوع ۴، تسویه‌ی نسیه و کارتخوان آزمایش شد).
"""
import json
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomUser
from holoo import wire
from holoo.client import HolooClient
from holoo.invoice import build_return_payload, payload_total, return_comment, return_item_lines, split_unit_amounts
from holoo.tasks import flag_rejected_return_with_invoice, reconcile_holoo_orders, send_return_to_holoo
from holoo.tests_real_write import real_config, response
from notifications.models import Notification
from payments.models import Transaction
from products.models import SiteSettings
from returns import services
from returns.models import ReturnRequest
from returns.tests_services import ServiceTestBase


class ReturnHolooBase(ServiceTestBase):
    def setUp(self):
        super().setUp()
        self.addCleanup(cache.delete, SiteSettings.CACHE_KEY)
        for patch_target in ('holoo.receivers.send_return_to_holoo.delay', 'notifications.tasks.deliver_notification.delay'):
            patcher = mock.patch(patch_target)
            patcher.start()
            self.addCleanup(patcher.stop)
        CustomUser.objects.filter(pk=self.user.pk).update(erp_code='CUST=')
        self.user.refresh_from_db()
        self.order.holoo_invoice_id = '1146'
        self.order.save(update_fields=['holoo_invoice_id'])
        self.admin = CustomUser.objects.create_superuser(phone_number='09140009000')

    def to_refund_pending(self, quantity=2, approved=None):
        request = self.create_request(quantity=quantity)
        services.approve_return_request(request, self.admin)
        item = request.items.get()
        services.mark_items_received(request, {item.pk: quantity if approved is None else approved}, self.admin)
        return services.mark_refund_pending(request)

    def pay_order(self):
        Transaction.objects.create(user=self.user, order=self.order, amount=150000, status='success', authority=f'AUTH-RET-{self.order.id}')

    def run_task(self, return_request, **result):
        with mock.patch('holoo.client.HolooClient.insert_invoice') as insert:
            insert.return_value = result or {'success': True, 'InvoiceCode': '1762', 'ErpCode': 'RET=', 'SanadCode': '27609', 'adopted': False}
            outcome = send_return_to_holoo.run(return_request.pk)
        return outcome, insert


class PayloadTests(ReturnHolooBase):
    def test_amounts_split_into_integer_prices_with_an_exact_total(self):
        for amount, quantity in ((100000, 2), (100001, 3), (7, 3), (1, 1)):
            with self.subTest(amount=amount, quantity=quantity):
                parts = split_unit_amounts(amount, quantity)
                self.assertEqual(sum(price * q for price, q in parts), amount)
                self.assertEqual(sum(q for _, q in parts), quantity)
                self.assertLessEqual(len(parts), 2)

    def test_payload_is_a_type_2_credit_invoice_with_numeric_id_and_the_frozen_amount(self):
        request = self.to_refund_pending(quantity=2)
        lines, missing = return_item_lines(request)
        payload = build_return_payload(request, lines, '999999', pos_sarfasl='10200010004')
        self.assertEqual((payload['Type'], payload['OrderId'], payload['CustomerErpCode'], payload['Paid']), (2, request.pk, 'CUST=', False))
        self.assertEqual(missing, [])
        self.assertEqual(len(payload['Items']), 1)
        self.assertEqual((payload['Items'][0]['ErpCode'], payload['Items'][0]['Amount'], payload['Items'][0]['Price']), ('ERP-RETURNS-1', 2, 50000.0))
        self.assertEqual(payload_total(payload), request.total_refund_amount)
        self.assertIn('مغایرت با تصویر', payload['Items'][0]['Comment'])
        self.assertEqual(payload['IssuedAt'], timezone.localtime(request.item_received_at))

    def test_comment_links_the_return_to_its_order_and_the_original_invoice(self):
        request = self.to_refund_pending()
        text = return_comment(request)
        for wanted in (f'مرجوعی #{request.pk}', f'سفارش #{self.order.pk}', 'فاکتور فروش هلو شماره 1146', 'مشتری: علی رضایی'):
            self.assertIn(wanted, text)

    def test_a_paid_order_is_settled_through_the_pos_like_the_sale_invoice(self):
        self.pay_order()
        request = self.to_refund_pending()
        body = wire.invoice_body(build_return_payload(request, return_item_lines(request)[0], '999999', pos_sarfasl='10200010004'))
        info = body['invoiceinfo'][0]
        self.assertEqual((info['type'], info['Bank'], info['BankSarfasl']), (2, 100000, '10200010004'))
        self.assertNotIn('Nesiyeh', info)

    def test_an_unpaid_cheque_order_is_settled_as_credit_like_the_owners_sample(self):
        request = self.to_refund_pending()
        info = wire.invoice_body(build_return_payload(request, return_item_lines(request)[0], '999999'))['invoiceinfo'][0]
        self.assertEqual((info['type'], info['Nesiyeh'], info['id']), (2, 100000, str(request.pk)))
        self.assertNotIn('Bank', info)

    def test_shipping_refund_adds_one_shipping_row(self):
        Order = type(self.order)
        Order.objects.filter(pk=self.order.pk).update(shipping_cost=45000)
        self.order.refresh_from_db()
        request = self.to_refund_pending(quantity=3)             # کل سفارش برمی‌گردد ← کرایه هم
        self.assertTrue(request.shipping_refunded)
        payload = build_return_payload(request, return_item_lines(request)[0], '999999')
        self.assertEqual([(row['ErpCode'], row['Amount'], row['Price']) for row in payload['Items']],
                         [('ERP-RETURNS-1', 3, 50000.0), ('999999', 1, 45000.0)])
        self.assertEqual(payload_total(payload), request.total_refund_amount)

    def test_rows_with_zero_approved_quantity_are_left_out(self):
        request = self.to_refund_pending(quantity=2, approved=0)
        self.assertEqual(return_item_lines(request), ([], []))

    def test_wire_default_type_is_still_a_sale(self):
        payload = {'OrderId': 5, 'CustomerErpCode': 'C=', 'IssuedAt': timezone.localtime(), 'Comment': '', 'Paid': False, 'Items': []}
        self.assertEqual(wire.invoice_body(payload)['invoiceinfo'][0]['type'], 1)


class ClientTests(ReturnHolooBase):
    def holoo_client(self, **extra):
        return HolooClient(real_config(**extra))

    def payload(self):
        request = self.to_refund_pending()
        return request, build_return_payload(request, return_item_lines(request)[0], '999999')

    def test_real_return_is_posted_as_type_2_with_the_numeric_id(self):
        request, payload = self.payload()
        client = self.holoo_client(client_id_prefix='8800')
        reply = response({'Header': 'Invoice', 'Success': {'Id': '1', 'ErpCode': 'RET=', 'Code': 1762, 'SanadCode': 74140}})
        with mock.patch('holoo.client.requests.post', return_value=reply) as post, mock.patch.object(HolooClient, '_get_auth_header', return_value='Bearer t'):
            result = client.insert_invoice(payload)
        sent = json.loads(post.call_args.kwargs['data'])['invoiceinfo'][0]
        self.assertEqual((sent['type'], sent['id']), (2, f'8800{request.pk}'))
        self.assertEqual((result['success'], result['InvoiceCode'], result['ErpCode']), (True, '1762', 'RET='))

    def test_duplicate_id_adopts_the_existing_return_found_in_the_type_4_listing(self):
        request, payload = self.payload()
        client = self.holoo_client()
        failure = response({'Header': 'Invoice', 'Failure': {'ErrorCode': 102, 'Error': 'duplicate'}})
        listing = {'invoice': [
            {'Code': 1762, 'ErpCode': 'RET=', 'CustomerErpCode': 'CUST=', 'SumPrice': 100000.0, 'SanadCode': 27609,
             'comment': f'برگشت از فروش سايت - مرجوعي #{request.pk} - سفارش #1'},
            {'Code': 1700, 'ErpCode': 'OTHER=', 'CustomerErpCode': 'CUST=', 'SumPrice': 100000.0, 'comment': f'مرجوعي #{request.pk}9 - سفارش #1'},
        ]}
        with mock.patch('holoo.client.requests.post', return_value=failure), mock.patch.object(HolooClient, '_get_auth_header', return_value='Bearer t'), \
             mock.patch.object(HolooClient, 'get_json', return_value=listing) as listed:
            result = client.insert_invoice(payload)
        self.assertEqual(listed.call_args.kwargs['params']['type'], wire.LIST_TYPE_SALE_RETURN)
        self.assertEqual((result['success'], result['adopted'], result['InvoiceCode']), (True, True, '1762'))

    def test_duplicate_without_a_match_asks_for_manual_review(self):
        _, payload = self.payload()
        failure = response({'Header': 'Invoice', 'Failure': {'ErrorCode': 102, 'Error': 'duplicate'}})
        with mock.patch('holoo.client.requests.post', return_value=failure), mock.patch.object(HolooClient, '_get_auth_header', return_value='Bearer t'), \
             mock.patch.object(HolooClient, 'get_json', return_value={'invoice': []}):
            result = self.holoo_client().insert_invoice(payload)
        self.assertFalse(result['success'])
        self.assertIn('بررسی دستی', result['message'])

    def test_comment_matcher_does_not_confuse_return_numbers(self):
        self.assertTrue(wire.comment_mentions_return('برگشت - مرجوعي #12 - سفارش #3', 12))
        self.assertFalse(wire.comment_mentions_return('برگشت - مرجوعي #123', 12))


class TaskTests(ReturnHolooBase):
    def test_success_stores_the_invoice_numbers(self):
        request = self.to_refund_pending()
        outcome, insert = self.run_task(request)
        self.assertEqual(outcome, 'Success: 1762')
        request.refresh_from_db()
        self.assertEqual((request.holoo_invoice_id, request.holoo_invoice_erp_code, request.holoo_needs_attention), ('1762', 'RET=', False))
        sent = insert.call_args[0][0]
        self.assertEqual((sent['Type'], sent['OrderId']), (2, request.pk))

    def test_it_is_idempotent(self):
        request = self.to_refund_pending()
        self.run_task(request)
        outcome, insert = self.run_task(request)
        self.assertTrue(outcome.startswith('Already registered'))
        insert.assert_not_called()

    def test_a_completed_return_that_missed_registration_is_still_registered(self):
        request = self.to_refund_pending()
        services.complete_refund(request, self.admin)
        outcome, _ = self.run_task(request)
        self.assertEqual(outcome, 'Success: 1762')

    def test_returns_not_yet_in_the_refund_queue_or_rejected_are_never_registered(self):
        pending = self.create_request(quantity=1)
        outcome, insert = self.run_task(pending)
        self.assertTrue(outcome.startswith('Not ready'))
        insert.assert_not_called()

    def test_it_waits_until_the_sale_invoice_exists_in_holoo(self):
        request = self.to_refund_pending()
        type(self.order).objects.filter(pk=self.order.pk).update(holoo_invoice_id=None)
        with mock.patch('holoo.tasks.send_return_to_holoo.retry', side_effect=RuntimeError('retried')), \
             mock.patch('holoo.client.HolooClient.insert_invoice') as insert:
            with self.assertRaises(RuntimeError):
                send_return_to_holoo.run(request.pk)
        insert.assert_not_called()
        self.assertIn('فاکتور فروش', ReturnRequest.objects.get(pk=request.pk).holoo_last_error)

    def test_a_permanent_error_flags_the_return_and_alerts_the_admin_once(self):
        request = self.to_refund_pending()
        with mock.patch('notifications.service.notify_admin') as alert:
            outcome, _ = self.run_task(request, success=False, code='31', message='نوع نامعتبر', transient=False)
            self.run_task(request, success=False, code='31', message='نوع نامعتبر', transient=False)
        self.assertTrue(outcome.startswith('Needs attention'))
        request.refresh_from_db()
        self.assertTrue(request.holoo_needs_attention)
        self.assertIn('نوع نامعتبر', request.holoo_last_error)
        self.assertEqual(alert.call_count, 1)
        self.assertEqual(alert.call_args.args[0], 'holoo_return_needs_attention_admin')

    def test_a_transient_error_retries_and_notes_the_reason(self):
        request = self.to_refund_pending()
        with mock.patch('holoo.tasks.send_return_to_holoo.retry', side_effect=RuntimeError('retried')):
            with self.assertRaises(RuntimeError):
                self.run_task(request, success=False, code='NETWORK', message='هلو خاموش است', transient=True)
        request.refresh_from_db()
        self.assertFalse(request.holoo_needs_attention)
        self.assertIn('هلو خاموش است', request.holoo_last_error)

    def test_a_product_without_a_holoo_code_needs_manual_attention(self):
        request = self.to_refund_pending()
        type(self.product).objects.filter(pk=self.product.pk).update(erp_code='')
        with mock.patch('notifications.service.notify_admin'):
            outcome, insert = self.run_task(request)
        self.assertTrue(outcome.startswith('Needs attention'))
        insert.assert_not_called()

    def test_nothing_is_registered_when_every_approved_quantity_is_zero(self):
        request = self.to_refund_pending(approved=0)
        outcome, insert = self.run_task(request)
        self.assertEqual(outcome, 'Nothing to register.')
        insert.assert_not_called()

    def test_writes_disabled_skips_without_touching_holoo(self):
        request = self.to_refund_pending()
        with mock.patch('holoo.tasks._writes_disabled', return_value=True):
            outcome, insert = self.run_task(request)
        self.assertTrue(outcome.startswith('Skipped'))
        insert.assert_not_called()

    def test_a_customer_without_a_holoo_account_is_created_first(self):
        request = self.to_refund_pending()
        CustomUser.objects.filter(pk=self.user.pk).update(erp_code='')
        with mock.patch('holoo.tasks.sync_customer', return_value={'success': False, 'transient': False, 'message': 'کد ملی تکراری'}), \
             mock.patch('notifications.service.notify_admin'):
            outcome, insert = self.run_task(request)
        self.assertTrue(outcome.startswith('Needs attention'))
        insert.assert_not_called()


class SignalWiringTests(ReturnHolooBase):
    def test_entering_the_refund_queue_registers_the_return_but_earlier_steps_do_not(self):
        with mock.patch('holoo.receivers.send_return_to_holoo.delay') as delay, self.captureOnCommitCallbacks(execute=True):
            request = self.create_request(quantity=2)
            services.approve_return_request(request, self.admin)
            item = request.items.get()
            services.mark_items_received(request, {item.pk: 2}, self.admin)
            delay.assert_not_called()                                    # نه تأیید ادمین، نه دریافت کالا
            services.mark_refund_pending(request)
        delay.assert_called_once_with(request.pk)

    def test_completing_the_refund_does_not_register_again(self):
        request = self.to_refund_pending()
        with mock.patch('holoo.receivers.send_return_to_holoo.delay') as delay, self.captureOnCommitCallbacks(execute=True):
            services.complete_refund(request, self.admin)
        delay.assert_not_called()

    def test_rejecting_before_registration_changes_nothing_in_holoo(self):
        request = self.to_refund_pending()
        with mock.patch('notifications.service.notify_admin') as alert, self.captureOnCommitCallbacks(execute=True):
            services.reject_return_request(request, 'منصرف شد', self.admin)
        alert.assert_not_called()
        self.assertFalse(ReturnRequest.objects.get(pk=request.pk).holoo_needs_attention)

    def test_rejecting_after_registration_flags_it_for_manual_deletion_in_holoo(self):
        request = self.to_refund_pending()
        self.run_task(request)
        with mock.patch('notifications.service.notify_admin') as alert, self.captureOnCommitCallbacks(execute=True):
            services.reject_return_request(request, 'کالا آسیب‌دیده بود', self.admin)
        request.refresh_from_db()
        self.assertTrue(request.holoo_needs_attention)
        self.assertIn('دستی در هلو حذف کنید', request.holoo_last_error)
        self.assertIn('1762', request.holoo_last_error)
        self.assertEqual(alert.call_args.args[0], 'holoo_return_needs_attention_admin')

    def test_flag_helper_ignores_returns_that_are_not_rejected_or_not_registered(self):
        request = self.to_refund_pending()
        self.assertEqual(flag_rejected_return_with_invoice(request.pk), 'Nothing to flag.')
        self.assertEqual(flag_rejected_return_with_invoice(999999), 'Nothing to flag.')


class SafetyNetTests(ReturnHolooBase):
    def setUp(self):
        super().setUp()
        type(self.order_item).objects.filter(pk=self.order_item.pk).update(quantity=10)
        self.order_item.refresh_from_db()

    def test_reconcile_requeues_only_unregistered_returns_in_the_refund_queue(self):
        waiting = self.to_refund_pending()
        registered = self.to_refund_pending_second()
        ReturnRequest.objects.filter(pk=registered.pk).update(holoo_invoice_id='1763')
        flagged = self.to_refund_pending_second()
        ReturnRequest.objects.filter(pk=flagged.pk).update(holoo_needs_attention=True)
        with mock.patch('holoo.tasks.send_return_to_holoo.delay') as delay:
            reconcile_holoo_orders()
        self.assertEqual([call.args[0] for call in delay.call_args_list], [waiting.pk])

    def to_refund_pending_second(self):
        request = services.create_return_request(
            self.order, self.user, [{'order_item': self.order_item, 'reason': self.reason, 'requested_quantity': 1}],
            refund_method=ReturnRequest.REFUND_WALLET)
        services.approve_return_request(request, self.admin)
        services.mark_items_received(request, {request.items.get().pk: 1}, self.admin)
        return services.mark_refund_pending(request)


class AdminTests(ReturnHolooBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)
        self.url = reverse('admin:returns_returnrequest_changelist')

    def act(self, action, request):
        return self.client.post(self.url, {'action': action, '_selected_action': [request.pk]}, follow=True)

    def test_list_shows_the_holoo_state_per_stage(self):
        pending = self.create_request(quantity=1)
        queued = self.to_refund_pending()
        ReturnRequest.objects.filter(pk=queued.pk).update(holoo_last_error='آخرین تلاش ناموفق (موقت): هلو خاموش')
        body = self.client.get(self.url).content.decode()
        self.assertIn('در صف ثبت در هلو', body)
        self.assertIn('هلو خاموش', body)
        ReturnRequest.objects.filter(pk=queued.pk).update(holoo_invoice_id='1762')
        self.assertIn('ثبت شد (شماره 1762)', self.client.get(self.url).content.decode())
        ReturnRequest.objects.filter(pk=queued.pk).update(holoo_invoice_id='', holoo_needs_attention=True, holoo_last_error='کد ۳۱')
        self.assertIn('⚠ نیاز به بررسی: کد ۳۱', self.client.get(self.url).content.decode())

    def test_retry_action_clears_the_flag_and_queues_the_task(self):
        request = self.to_refund_pending()
        ReturnRequest.objects.filter(pk=request.pk).update(holoo_needs_attention=True, holoo_last_error='خطا')
        with mock.patch('holoo.tasks.send_return_to_holoo.delay') as delay, mock.patch('holoo.conf.HolooConfig.write_is_disabled', new_callable=mock.PropertyMock, return_value=False):
            response_ = self.act('retry_holoo_registration_action', request)
        delay.assert_called_once_with(request.pk)
        self.assertContains(response_, 'ثبت 1 فاکتور برگشتی در هلو به صف رفت')
        request.refresh_from_db()
        self.assertEqual((request.holoo_needs_attention, request.holoo_last_error), (False, ''))

    def test_retry_action_skips_registered_and_early_stage_returns(self):
        registered = self.to_refund_pending()
        ReturnRequest.objects.filter(pk=registered.pk).update(holoo_invoice_id='1762')
        with mock.patch('holoo.tasks.send_return_to_holoo.delay') as delay, mock.patch('holoo.conf.HolooConfig.write_is_disabled', new_callable=mock.PropertyMock, return_value=False):
            response_ = self.act('retry_holoo_registration_action', registered)
        delay.assert_not_called()
        self.assertContains(response_, 'انتخاب نشد')

    def test_completing_before_registration_warns_but_still_pays_the_customer(self):
        request = self.to_refund_pending()
        response_ = self.act('complete_refund_action', request)
        request.refresh_from_db()
        self.assertEqual(request.status, ReturnRequest.STATUS_COMPLETED)
        self.assertContains(response_, 'هنوز در هلو ثبت نشده است')

    def test_completing_after_registration_has_no_warning(self):
        request = self.to_refund_pending()
        ReturnRequest.objects.filter(pk=request.pk).update(holoo_invoice_id='1762')
        self.assertNotContains(self.act('complete_refund_action', request), 'هنوز در هلو ثبت نشده است')

    def test_change_page_shows_the_holoo_fields_read_only(self):
        request = self.to_refund_pending()
        page = self.client.get(reverse('admin:returns_returnrequest_change', args=[request.pk]))
        self.assertContains(page, 'شماره فاکتور برگشتی در هلو')
        self.assertContains(page, 'در صف ثبت در هلو')
