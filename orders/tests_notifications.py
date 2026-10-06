"""
پیام‌های سفارش برای مشتری: تأیید مدیر، و رد به‌دلیل نبود موجودی (با/بی‌پرداخت).
"""
from unittest import mock

from django.test import TestCase

from notifications.models import Notification
from orders.approval import approve_order
from orders.stock_hooks import reject_for_stock
from orders.tests_approval import ApprovalBase


class OrderCustomerMessagesTests(ApprovalBase):
    def setUp(self):
        super().setUp()
        type(self.user).objects.filter(pk=self.user.pk).update(first_name='علی')
        self.user.refresh_from_db()
        patcher = mock.patch('notifications.tasks.deliver_notification.delay')
        patcher.start()
        self.addCleanup(patcher.stop)

    def sent(self, key):
        return list(Notification.objects.filter(template_key=key))

    def test_approving_an_order_tells_the_customer(self):
        order = self.make_order('cash', paid=True)
        with mock.patch('holoo.receivers.send_order_to_holoo.delay'), self.captureOnCommitCallbacks(execute=True):
            approve_order(order, by=self.admin)
        (message,) = self.sent('order_approved_customer')
        self.assertEqual(message.recipient, self.user.phone_number)
        self.assertIn(str(order.id), message.text)

    def test_a_paid_order_rejected_for_stock_tells_the_customer_about_the_refund(self):
        order = self.make_order('cash', paid=True)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertTrue(reject_for_stock(order, 'نبود موجودی'))
        (message,) = self.sent('order_rejected_stock_customer')
        self.assertEqual(message.recipient, self.user.phone_number)
        self.assertIn('بازگشت مبلغ پرداختی', message.text)

    def test_an_unpaid_order_rejected_for_stock_has_no_refund_sentence(self):
        order = self.make_order('check', paid=False)
        with self.captureOnCommitCallbacks(execute=True):
            reject_for_stock(order, 'نبود موجودی')
        (message,) = self.sent('order_rejected_stock_customer')
        self.assertNotIn('بازگشت مبلغ', message.text)

    def test_rejecting_twice_sends_one_message(self):
        order = self.make_order('cash', paid=True)
        with self.captureOnCommitCallbacks(execute=True):
            reject_for_stock(order, 'x')
            reject_for_stock(order, 'x')
        self.assertEqual(len(self.sent('order_rejected_stock_customer')), 1)
