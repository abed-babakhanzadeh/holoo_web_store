"""تست یکپارچه‌سازی حسابداری — idempotency، قفل، و بازبینی سفارش‌های جامانده."""

from unittest import mock

from django.core.cache import cache
from django.test import TestCase

from accounts.models import CustomUser
from holoo.locks import task_lock
from holoo.tasks import confirm_payment_in_holoo, reconcile_holoo_orders, send_order_to_holoo
from orders.models import Order, OrderItem
from payments.models import Transaction
from products.models import Category, Product


class TaskLockTests(TestCase):
    def setUp(self):
        cache.delete('lock:test-lock')

    def test_second_holder_is_refused_then_released(self):
        with task_lock('test-lock', timeout=30) as first:
            self.assertTrue(first)
            with task_lock('test-lock', timeout=30) as second:
                self.assertFalse(second)
        with task_lock('test-lock', timeout=30) as third:
            self.assertTrue(third)

    def test_lock_is_released_even_if_body_raises(self):
        with self.assertRaises(RuntimeError):
            with task_lock('test-lock', timeout=30):
                raise RuntimeError('boom')
        with task_lock('test-lock', timeout=30) as again:
            self.assertTrue(again)


class OrderSyncTests(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(phone_number='09120000040', erp_code='CUST-1')
        category = Category.objects.create(name='تست', slug='holoo-test-cat')
        self.product = Product.objects.create(
            name='کالا', slug='holoo-test-product', erp_code='ERP-H-1',
            category=category, price=100000, stock=5,
        )
        self.order = Order.objects.create(
            user=self.user, first_name='علی', last_name='رضایی', phone='09120000040',
            address='تهران', payment_method='cash', shipping_cost=200000, total_price=300000,
        )
        OrderItem.objects.create(order=self.order, product=self.product, price=100000, quantity=1)
        for key in ('lock:holoo:invoice:%s' % self.order.id, 'lock:holoo:receipt:%s' % self.order.id):
            cache.delete(key)

    def _pay(self):
        return Transaction.objects.create(
            user=self.user, order=self.order, amount=self.order.total_price,
            authority='AUTH-%s' % self.order.id, ref_id='REF1', status='success',
        )

    # --- ثبت فاکتور ---

    def test_invoice_is_registered_and_status_advances(self):
        send_order_to_holoo(self.order.id)
        self.order.refresh_from_db()

        self.assertTrue(self.order.holoo_invoice_id)
        self.assertEqual(self.order.status, 'registered')

    def test_already_registered_order_is_not_sent_again(self):
        """ بدون این محافظ، هر تلاش مجدد یک فاکتور تازه در حسابداری می‌ساخت """
        send_order_to_holoo(self.order.id)
        self.order.refresh_from_db()
        first_invoice = self.order.holoo_invoice_id

        with mock.patch('holoo.client.HolooClient.insert_invoice') as insert:
            result = send_order_to_holoo(self.order.id)

        self.assertEqual(insert.call_count, 0)
        self.assertIn('Already registered', result)
        self.order.refresh_from_db()
        self.assertEqual(self.order.holoo_invoice_id, first_invoice)

    def test_order_without_sendable_items_is_not_sent(self):
        self.order.items.all().delete()
        with mock.patch('holoo.client.HolooClient.insert_invoice') as insert:
            send_order_to_holoo(self.order.id)
        self.assertEqual(insert.call_count, 0)

    def test_missing_order_does_not_retry_forever(self):
        self.assertEqual(send_order_to_holoo(999999), 'Order not found.')

    # --- سند دریافت وجه ---

    def test_receipt_requires_a_successful_payment(self):
        self.order.holoo_invoice_id = 'INV-1'
        self.order.save(update_fields=['holoo_invoice_id'])
        self.assertEqual(confirm_payment_in_holoo(self.order.id), 'Not paid.')

    def test_receipt_is_registered_and_status_advances(self):
        self._pay()
        self.order.holoo_invoice_id = 'INV_123'
        self.order.save(update_fields=['holoo_invoice_id'])

        confirm_payment_in_holoo(self.order.id)
        self.order.refresh_from_db()

        self.assertTrue(self.order.holoo_receipt_id)
        self.assertEqual(self.order.status, 'processing')

    def test_receipt_is_not_registered_twice(self):
        self._pay()
        self.order.holoo_invoice_id = 'INV_123'
        self.order.save(update_fields=['holoo_invoice_id'])
        confirm_payment_in_holoo(self.order.id)

        with mock.patch('holoo.client.HolooClient.register_payment') as register:
            result = confirm_payment_in_holoo(self.order.id)

        self.assertEqual(register.call_count, 0)
        self.assertIn('Already registered', result)

    def test_payment_before_invoice_retries_instead_of_giving_up(self):
        """
        باگ اصلی نسخه‌ی قبل: اگر کاربر زودتر از ثبت فاکتور پرداخت می‌کرد، تسک با
        "No Invoice" برمی‌گشت و سند دریافت وجه آن سفارش برای همیشه گم می‌شد.
        """
        self._pay()
        self.assertIsNone(self.order.holoo_invoice_id)

        with mock.patch('holoo.tasks.confirm_payment_in_holoo.retry', side_effect=RuntimeError('retried')):
            with self.assertRaises(RuntimeError):
                confirm_payment_in_holoo(self.order.id)

    # --- تور ایمنی ---

    def test_reconcile_requeues_orders_missing_invoice(self):
        Order.objects.filter(pk=self.order.pk).update(
            created_at=self.order.created_at.replace(year=self.order.created_at.year - 1)
        )
        with mock.patch('holoo.tasks.send_order_to_holoo.delay') as task:
            reconcile_holoo_orders()
        self.assertIn(self.order.id, [c.args[0] for c in task.call_args_list])

    def test_reconcile_skips_recent_orders(self):
        """ سفارش تازه‌ثبت‌شده هنوز در چرخه‌ی عادی است و نباید دوباره به صف برود """
        with mock.patch('holoo.tasks.send_order_to_holoo.delay') as task:
            reconcile_holoo_orders()
        self.assertNotIn(self.order.id, [c.args[0] for c in task.call_args_list])

    def test_reconcile_skips_canceled_orders(self):
        Order.objects.filter(pk=self.order.pk).update(
            status='canceled',
            created_at=self.order.created_at.replace(year=self.order.created_at.year - 1),
        )
        with mock.patch('holoo.tasks.send_order_to_holoo.delay') as task:
            reconcile_holoo_orders()
        self.assertNotIn(self.order.id, [c.args[0] for c in task.call_args_list])
