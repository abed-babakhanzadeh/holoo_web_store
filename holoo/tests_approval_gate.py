"""
ثبت فاکتور هلو فقط پس از تأیید مدیر؛ رد سفارش با خطای ۲۸ هلو؛ تسک بازبینی و رویداد order_approved.
(نویسنده‌ی واحد وضعیت کالا: tests_product_state.py)
"""
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from accounts.models import CustomUser
from holoo.tasks import confirm_payment_in_holoo, reconcile_holoo_orders, send_order_to_holoo
from orders.models import Order, OrderItem
from orders.signals import order_approved, order_placed
from payments.models import Transaction
from products import stock
from products.models import Category, Product, StockReservation


class ApprovalGatedInvoiceTests(TestCase):
    """ فاکتور قطعی هلو فقط پس از «تأیید مدیر»؛ ثبت سفارش و پرداخت به‌تنهایی چیزی به هلو نمی‌فرستد """

    def setUp(self):
        self.user = CustomUser.objects.create_user(phone_number='09120000900', erp_code='CUST-9')
        category = Category.objects.create(name='تأیید', slug='appr-cat')
        self.product = Product.objects.create(name='کالا', slug='appr-p', erp_code='ERP-APPR-1', category=category,
                                              price=100000, stock=5)
        self.order = Order.objects.create(user=self.user, first_name='علی', last_name='رضایی', phone='09120000900',
                                          address='تهران', payment_method='cash', total_price=200000)
        OrderItem.objects.create(order=self.order, product=self.product, price=100000, quantity=2)
        with stock.transaction.atomic():
            stock.reserve_for_order(self.order.id, {self.product.pk: 2})
        cache.delete('lock:holoo:invoice:%s' % self.order.id)
        cache.delete('lock:holoo:receipt:%s' % self.order.id)

    def approve(self):
        Order.objects.filter(pk=self.order.pk).update(approved_at=timezone.now())

    def send(self, **result):
        with mock.patch('holoo.client.HolooClient.insert_invoice') as insert:
            insert.return_value = result or {'success': True, 'InvoiceCode': 'INV-1'}
            outcome = send_order_to_holoo(self.order.id)
        return outcome, insert

    def test_unapproved_order_is_never_sent(self):
        outcome, insert = self.send()
        self.assertEqual(outcome, 'Awaiting admin approval.')
        insert.assert_not_called()
        self.assertFalse(Order.objects.get(pk=self.order.pk).holoo_invoice_id)

    def test_canceled_or_rejected_orders_are_never_sent_even_if_approved(self):
        self.approve()
        for status in ('canceled', 'rejected_stock'):
            Order.objects.filter(pk=self.order.pk).update(status=status)
            outcome, insert = self.send()
            self.assertEqual(outcome, 'Order is not active.')
            insert.assert_not_called()

    def test_approved_order_is_sent_registered_and_its_reservation_waits_for_the_next_sync(self):
        self.approve()
        outcome, insert = self.send()
        insert.assert_called_once()
        order = Order.objects.get(pk=self.order.pk)
        self.assertEqual((order.holoo_invoice_id, order.status), ('INV-1', 'registered'))
        row = StockReservation.objects.get(order_id=order.id)
        self.assertEqual(row.state, 'invoiced')
        self.assertIsNotNone(row.invoiced_at)
        self.assertEqual(Product.objects.get(pk=self.product.pk).reserved_quantity, 2)       # هنوز نگه داشته شده

    def test_holoo_error_28_rejects_the_order_for_stock_without_retrying_and_alerts_the_admin(self):
        self.approve()
        with mock.patch('notifications.service.notify_admin') as alert:
            outcome, insert = self.send(success=False, code='28', message='کالاهای زیر فاقد موجودی میباشد')
        self.assertEqual(outcome, 'Rejected: stock (Holoo error 28)')
        order = Order.objects.get(pk=self.order.pk)
        self.assertEqual(order.status, 'rejected_stock')
        self.assertFalse(order.holoo_invoice_id)
        self.assertEqual(StockReservation.objects.get(order_id=order.id).state, 'released')
        self.assertEqual(Product.objects.get(pk=self.product.pk).reserved_quantity, 0)
        alert.assert_called_once()
        self.assertEqual(alert.call_args.args[0], 'critical_alert')

    def test_other_holoo_errors_still_retry(self):
        self.approve()
        with mock.patch('holoo.tasks.send_order_to_holoo.retry', side_effect=RuntimeError('retried')):
            with self.assertRaises(RuntimeError):
                self.send(success=False, code='15', message='کد کالا معتبر نیست')
        self.assertEqual(Order.objects.get(pk=self.order.pk).status, 'pending')

    def test_receipt_waits_for_approval_without_retry_noise(self):
        Transaction.objects.create(user=self.user, order=self.order, amount=200000, authority='AUTH-APPR-1', status='success')
        with mock.patch('holoo.client.HolooClient.register_payment') as register, \
             mock.patch('holoo.tasks.confirm_payment_in_holoo.retry', side_effect=RuntimeError('retried')):
            outcome = confirm_payment_in_holoo(self.order.id)
        self.assertEqual(outcome, 'Awaiting admin approval.')
        register.assert_not_called()

    def test_reconcile_requeues_only_approved_active_orders(self):
        Order.objects.filter(pk=self.order.pk).update(created_at=timezone.now() - timedelta(hours=2))
        with mock.patch('holoo.tasks.send_order_to_holoo.delay') as task:
            reconcile_holoo_orders()
        task.assert_not_called()                                                          # تأییدنشده

        self.approve()
        with mock.patch('holoo.tasks.send_order_to_holoo.delay') as task:
            reconcile_holoo_orders()
        task.assert_called_once_with(self.order.id)

        Order.objects.filter(pk=self.order.pk).update(status='rejected_stock')
        with mock.patch('holoo.tasks.send_order_to_holoo.delay') as task:
            reconcile_holoo_orders()
        task.assert_not_called()

    def test_only_the_approval_event_triggers_accounting(self):
        with mock.patch('holoo.receivers.send_order_to_holoo') as task:
            order_placed.send(sender=Order, order=self.order)
            task.delay.assert_not_called()
            order_approved.send(sender=Order, order=self.order)
            task.delay.assert_called_once_with(self.order.id)
