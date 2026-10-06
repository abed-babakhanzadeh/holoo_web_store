"""
هر مرحله‌ی مرجوعی یک پیام برای مشتری دارد (ثبت ← تأیید ← دریافت کالا ← صف بازپرداخت ← تکمیل، یا رد)، و ثبت درخواست به مدیر هم
خبر می‌دهد. همچنین دو پیام سفارش: تأیید مدیر و رد به‌دلیل نبود موجودی.
"""
from unittest import mock

from django.test import TestCase, override_settings

from accounts.models import UserBankAccount
from notifications.models import Notification
from returns.models import ReturnRequest
from returns.services import (
    approve_return_request, complete_refund, create_return_request, mark_items_received, mark_refund_pending,
    reject_return_request,
)
from returns.tests import ReturnsTestMixin


@override_settings(ADMIN_NOTIFICATION_RECIPIENT='09120000000')
class ReturnLifecycleNotificationTests(ReturnsTestMixin, TestCase):
    def setUp(self):
        self.user = self.make_user()
        type(self.user).objects.filter(pk=self.user.pk).update(first_name='علی')
        self.user.refresh_from_db()
        self.order = self.make_order(self.user)
        self.product = self.make_product(self.make_category())
        self.order_item = self.make_order_item(self.order, self.product, price=50000, quantity=3)
        self.reason = self.make_reason()
        patcher = mock.patch('notifications.tasks.deliver_notification.delay')
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_step(self, fn, *args, **kwargs):
        with self.captureOnCommitCallbacks(execute=True):
            return fn(*args, **kwargs)

    def sent(self, key):
        return list(Notification.objects.filter(template_key=key).order_by('id'))

    def request(self, method=ReturnRequest.REFUND_WALLET, **kwargs):
        return self.run_step(create_return_request, self.order, self.user,
                             [{'order_item': self.order_item, 'reason': self.reason, 'requested_quantity': 2}],
                             refund_method=method, **kwargs)

    def test_creating_a_request_tells_the_customer_and_the_admin(self):
        self.request()
        (customer,) = self.sent('return_requested_customer')
        (admin,) = self.sent('return_requested_admin')
        self.assertEqual(customer.recipient, self.user.phone_number)
        self.assertIn(f'#{self.order.id}', customer.text)
        self.assertEqual(admin.recipient, '09120000000')

    def test_every_stage_of_a_wallet_refund_messages_the_customer(self):
        request = self.request()
        self.run_step(approve_return_request, request, self.user)
        item = request.items.get()
        self.run_step(mark_items_received, request, {item.pk: 2}, self.user)
        self.run_step(mark_refund_pending, request)
        self.run_step(complete_refund, request, self.user)

        keys = [n.template_key for n in Notification.objects.filter(recipient=self.user.phone_number).order_by('id')]
        self.assertEqual(keys, ['return_requested_customer', 'return_approved_customer', 'return_item_received_customer',
                                'return_refund_pending_customer', 'return_refund_completed_customer'])
        pending = self.sent('return_refund_pending_customer')[0]
        self.assertIn('100,000', pending.text)
        done = self.sent('return_refund_completed_customer')[0]
        self.assertIn('100,000', done.text)
        self.assertIn('کیف پول', done.text)

    def test_a_bank_refund_says_bank_account(self):
        account = UserBankAccount.objects.create(user=self.user, account_holder_first_name='علی', account_holder_last_name='رضایی',
                                                 card_number='6037991234567890')
        request = self.request(method=ReturnRequest.REFUND_BANK, bank_account=account)
        self.run_step(approve_return_request, request, self.user)
        self.run_step(mark_items_received, request, {request.items.get().pk: 2}, self.user)
        self.run_step(mark_refund_pending, request)
        self.run_step(complete_refund, request, self.user)
        self.assertIn('حساب بانکی', self.sent('return_refund_completed_customer')[0].text)

    def test_a_rejection_carries_the_reason(self):
        request = self.request()
        self.run_step(reject_return_request, request, 'کالا استفاده شده است', self.user)
        (message,) = self.sent('return_rejected_customer')
        self.assertIn('کالا استفاده شده است', message.text)
        self.assertEqual(self.sent('return_approved_customer'), [])

    def test_a_switched_off_stage_is_respected(self):
        from notifications.models import NotificationSetting, sync_notification_settings
        sync_notification_settings()
        NotificationSetting.objects.filter(template_key='return_item_received_customer').update(is_enabled=False)
        request = self.request()
        self.run_step(approve_return_request, request, self.user)
        self.run_step(mark_items_received, request, {request.items.get().pk: 2}, self.user)
        self.assertEqual(self.sent('return_item_received_customer'), [])
        self.assertEqual(len(self.sent('return_approved_customer')), 1)

    def test_every_return_template_in_the_registry_is_now_sent_by_something(self):
        import re
        from pathlib import Path

        from notifications.templates_registry import TEMPLATES
        source = (Path(__file__).resolve().parent.parent / 'notifications' / 'receivers.py').read_text(encoding='utf-8')
        for key in (k for k in TEMPLATES if k.startswith('return_')):
            self.assertTrue(re.search(rf"['\"]{key}['\"]", source), f'پیام «{key}» هیچ‌جا وصل نیست')
