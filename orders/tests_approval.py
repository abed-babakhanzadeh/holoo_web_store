"""
تأیید دومرحله‌ای سفارش توسط مدیر: قواعد قابل‌تأیید بودن، اتمی و یک‌باره بودن، رویداد order_approved پس از commit،
اکشن/دکمه‌ی پنل مدیریت و مایگریشن داده‌ی سفارش‌های قدیمی.
"""
from importlib import import_module
from unittest import mock

from django.apps import apps as django_apps
from django.contrib.admin.sites import site
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomUser
from orders.admin import OrderAdmin, ReviewFilter
from orders.approval import ApprovalError, approval_blocker, approve_order
from orders.models import ChequePayment, Order, OrderItem
from orders.signals import order_approved
from payments.models import Transaction
from products import stock
from products.models import Category, Product, StockReservation


class ApprovalBase(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(phone_number='09120000950', erp_code='CUST-A')
        self.admin = CustomUser.objects.create_superuser(phone_number='09120000951', password='x')
        category = Category.objects.create(name='تأیید', slug='approval-cat')
        self.product = Product.objects.create(name='کالا', slug='approval-p', erp_code='ERP-APPROVAL', category=category,
                                              price=100000, stock=100)

    _sayadi = 6219861000000000

    def make_order(self, method='cash', paid=True, reserve=True, cheque='approved', **fields):
        """ cheque: وضعیت چکِ ثبت‌شده برای سفارش چکی (پیش‌فرض approved؛ None = بدون چک) """
        order = Order.objects.create(user=self.user, first_name='علی', last_name='رضایی', phone='09120000950',
                                     address='تهران', payment_method=method, total_price=200000, **fields)
        if order.is_cheque and cheque:
            ApprovalBase._sayadi += 1
            ChequePayment.objects.create(order=order, sayadi_id=str(ApprovalBase._sayadi), status=cheque)
        OrderItem.objects.create(order=order, product=self.product, price=100000, quantity=2)
        if reserve:
            with stock.transaction.atomic():
                stock.reserve_for_order(order.id, {self.product.pk: 2})
        if paid:
            Transaction.objects.create(user=self.user, order=order, amount=200000, status='success',
                                       authority=f'AUTH-APPR-{Transaction.objects.count() + 1}')
        return order


class ApprovalRulesTests(ApprovalBase):
    def test_paid_online_and_cheque_orders_are_approvable(self):
        self.assertIsNone(approval_blocker(self.make_order('cash', paid=True)))
        self.assertIsNone(approval_blocker(self.make_order('vip', paid=True)))
        self.assertIsNone(approval_blocker(self.make_order('check', paid=False)))

    def test_unpaid_online_order_is_not_approvable(self):
        self.assertIn('پرداخت نشده', approval_blocker(self.make_order('cash', paid=False)))

    def test_canceled_rejected_and_advanced_orders_are_not_approvable(self):
        for status, fragment in (('canceled', 'لغو'), ('rejected_stock', 'نبود موجودی'), ('processing', 'قابل‌تأیید نیست')):
            order = self.make_order('cash', paid=True, status=status)
            self.assertIn(fragment, approval_blocker(order))

    def test_already_approved_order_is_not_approvable_again(self):
        order = self.make_order('check', paid=False, approved_at=timezone.now())
        self.assertIn('قبلاً تأیید', approval_blocker(order))

    def test_order_without_items_is_not_approvable(self):
        order = self.make_order('check', paid=False)
        order.items.all().delete()
        self.assertIn('ردیف', approval_blocker(order))


class ApproveOrderTests(ApprovalBase):
    def test_approval_stamps_the_order_and_announces_the_event_after_commit(self):
        order = self.make_order('cash')
        seen = []
        handler = lambda sender, order, **kw: seen.append(order.pk)          # noqa: E731
        order_approved.connect(handler, weak=False, dispatch_uid='test_seen')
        self.addCleanup(order_approved.disconnect, dispatch_uid='test_seen')
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            approve_order(order, by=self.admin)
            self.assertEqual(seen, [])                                       # پیش از commit رویدادی نیست
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertEqual(seen, [order.pk])
        order.refresh_from_db()
        self.assertIsNotNone(order.approved_at)
        self.assertEqual(order.approved_by, self.admin)
        self.assertEqual(order.customer_status, 'processing')

    def test_approving_twice_is_refused_and_fires_the_event_once(self):
        order = self.make_order('check', paid=False)
        fired = []
        handler = lambda sender, order, **kwargs: fired.append(order.pk)
        order_approved.connect(handler, weak=False)
        self.addCleanup(order_approved.disconnect, handler)
        with self.captureOnCommitCallbacks(execute=True):
            approve_order(order)
            with self.assertRaises(ApprovalError):
                approve_order(order)
        self.assertEqual(fired, [order.pk])     # رویداد دقیقاً یک بار (شنونده‌های دیگر مثل پیامک مشتری callback خودشان را دارند)

    def test_approval_triggers_the_accounting_task_only_through_the_event(self):
        order = self.make_order('cash')
        with mock.patch('holoo.receivers.send_order_to_holoo') as task, self.captureOnCommitCallbacks(execute=True):
            approve_order(order)
        task.delay.assert_called_once_with(order.id)

    def test_approval_keeps_the_reservation_without_a_deadline(self):
        order = self.make_order('check', paid=False)
        approve_order(order)
        row = StockReservation.objects.get(order_id=order.id)
        self.assertEqual((row.state, row.expires_at), ('held', None))

    def test_a_legacy_order_without_a_reservation_is_reserved_at_approval(self):
        order = self.make_order('check', paid=False, reserve=False)
        approve_order(order)
        self.assertEqual(Product.objects.get(pk=self.product.pk).reserved_quantity, 2)

    def test_a_legacy_order_cannot_be_approved_when_stock_is_gone(self):
        order = self.make_order('check', paid=False, reserve=False)
        Product.objects.filter(pk=self.product.pk).update(stock=1)
        with self.assertRaises(ApprovalError) as ctx:
            approve_order(order)
        self.assertIn('موجودی کافی نیست', str(ctx.exception))
        order.refresh_from_db()
        self.assertIsNone(order.approved_at)


class AdminTests(ApprovalBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def test_action_approves_eligible_orders_and_reports_the_rest(self):
        ok = self.make_order('cash', paid=True)
        unpaid = self.make_order('cash', paid=False)
        with mock.patch('holoo.receivers.send_order_to_holoo') as task, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse('admin:orders_order_changelist'), {
                'action': 'approve_orders', '_selected_action': [ok.pk, unpaid.pk],
            }, follow=True)
        ok.refresh_from_db()
        unpaid.refresh_from_db()
        self.assertIsNotNone(ok.approved_at)
        self.assertIsNone(unpaid.approved_at)
        self.assertContains(response, f'سفارش #{ok.pk} تأیید شد')
        self.assertContains(response, f'سفارش #{unpaid.pk} تأیید نشد')
        task.delay.assert_called_once_with(ok.pk)

    def test_change_page_shows_the_approve_button_only_for_pending_unapproved_orders(self):
        pending = self.make_order('check', paid=False)
        url = reverse('admin:orders_order_approve', args=[pending.pk])
        self.assertContains(self.client.get(reverse('admin:orders_order_change', args=[pending.pk])), url)
        Order.objects.filter(pk=pending.pk).update(approved_at=timezone.now())
        self.assertNotContains(self.client.get(reverse('admin:orders_order_change', args=[pending.pk])), url)

    def test_approve_view_is_post_only_and_approves(self):
        order = self.make_order('check', paid=False)
        url = reverse('admin:orders_order_approve', args=[order.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url, follow=True)
        order.refresh_from_db()
        self.assertIsNotNone(order.approved_at)
        self.assertContains(response, f'سفارش #{order.pk} تأیید شد')

    def test_a_staff_user_without_change_permission_cannot_approve(self):
        staff = CustomUser.objects.create_user(phone_number='09120000952', is_staff=True)
        order = self.make_order('check', paid=False)
        self.client.force_login(staff)
        response = self.client.post(reverse('admin:orders_order_approve', args=[order.pk]))
        self.assertIn(response.status_code, (302, 403))
        order.refresh_from_db()
        self.assertIsNone(order.approved_at)

    def test_review_filter_lists_only_orders_waiting_for_the_admin(self):
        waiting_paid = self.make_order('cash', paid=True)
        waiting_cheque = self.make_order('check', paid=False)
        self.make_order('cash', paid=False)                                    # آنلاینِ پرداخت‌نشده
        self.make_order('cash', paid=True, approved_at=timezone.now())         # تأییدشده
        response = self.client.get(reverse('admin:orders_order_changelist'), {'review': 'pending'})
        shown = {o.pk for o in response.context['cl'].result_list}
        self.assertEqual(shown, {waiting_paid.pk, waiting_cheque.pk})
        approved = self.client.get(reverse('admin:orders_order_changelist'), {'review': 'approved'})
        self.assertEqual(len(approved.context['cl'].result_list), 1)

    def test_approval_fields_are_read_only_in_the_admin(self):
        model_admin = site._registry[Order]
        self.assertIn('approved_at', model_admin.readonly_fields)
        self.assertIn('approved_by', model_admin.readonly_fields)
        self.assertIn('approve_orders', model_admin.actions)


class LegacyMigrationTests(ApprovalBase):
    def run_migration(self):
        module = import_module('orders.migrations.0015_mark_legacy_orders_approved')
        module.mark_legacy_orders_approved(django_apps, None)

    def test_orders_that_already_passed_the_old_automatic_flow_are_marked_approved(self):
        invoiced = self.make_order('cash', paid=True, holoo_invoice_id='INV-OLD')
        shipped = self.make_order('check', paid=False, status='shipped')
        waiting = self.make_order('cash', paid=True)                           # pending بدون فاکتور: از روال تازه می‌گذرد
        canceled = self.make_order('cash', paid=False, status='canceled')
        self.run_migration()
        for order in (invoiced, shipped, waiting, canceled):
            order.refresh_from_db()
        self.assertEqual(invoiced.approved_at, invoiced.created_at)
        self.assertEqual(shipped.approved_at, shipped.created_at)
        self.assertIsNone(waiting.approved_at)
        self.assertIsNone(canceled.approved_at)
        self.assertEqual(invoiced.customer_status, 'processing')

    def test_migration_is_idempotent(self):
        order = self.make_order('cash', paid=True, holoo_invoice_id='INV-OLD')
        self.run_migration()
        first = Order.objects.get(pk=order.pk).approved_at
        self.run_migration()
        self.assertEqual(Order.objects.get(pk=order.pk).approved_at, first)
