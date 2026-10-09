"""
فاز G3: وصول چک (cleared) و Order.cheque_settled_at، آزادسازی اعتبار با وصول، کسر مرجوعی قطعی از اعتبار درگیر، اکشن‌ها/دکمه‌ی ادمین
و نمایش وضعیت وصول برای ادمین و مشتری. MEDIA_ROOT هر تست موقت است؛ به media/ واقعی چیزی نوشته نمی‌شود.
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomUser
from accounts.testing import make_approved_user
from orders import cheques, credit, deadline
from orders.approval import approval_blocker
from orders.models import ChequePayment, Order, OrderItem
from orders.tests_cheque_review import ReviewBase
from orders.tests_cheques import VALID, make_image, upload
from orders.tests_credit_limit import make_order, set_limit
from products import stock
from returns.models import ReturnItem, ReturnReason, ReturnRequest

OTHER = '6219861099999999'
THIRD = '6219861088888888'


class ClearingBase(ReviewBase):
    """ ReviewBase: سفارش چکی با یک چک (در انتظار) + ادمین. این‌جا چک را تأیید می‌کنیم تا قابل وصول باشد """

    def setUp(self):
        super().setUp()
        self.approve()

    def add_cheque(self, sayadi=OTHER, status='approved'):
        cheque = cheques.create_cheque(self.order, self.user, {'sayadi_id': sayadi}, [upload(make_image())])
        if status == 'approved':
            cheques.set_review(cheque, 'approved', self.admin_user)
        elif status == 'rejected':
            cheques.set_review(cheque, 'rejected', self.admin_user, 'ناخوانا')
        return self.refresh(cheque)

    def clear(self, cheque=None):
        return cheques.clear_cheque(cheque or self.cheque, self.admin_user)

    def settled(self):
        return self.refresh(self.order).cheque_settled_at


# ------------------------------------------------------------------ سرویس وصول
class ClearChequeTests(ClearingBase):
    def test_clearing_an_approved_cheque_stamps_it_and_settles_the_single_cheque_order(self):
        before = timezone.now()
        cleared = self.clear()
        self.assertEqual(cleared.status, 'cleared')
        self.assertGreaterEqual(cleared.cleared_at, before)
        self.assertEqual(self.settled(), cleared.cleared_at)

    def test_a_multi_cheque_order_settles_only_when_the_last_one_clears(self):
        second = self.add_cheque(OTHER)
        third = self.add_cheque(THIRD)
        self.clear(self.cheque)
        self.clear(second)
        self.assertIsNone(self.settled())
        last = self.clear(third)
        self.assertEqual(self.settled(), last.cleared_at)                    # جدیدترین وصول

    def test_the_settled_time_is_the_latest_clearing(self):
        second = self.add_cheque(OTHER)
        first = self.clear(self.cheque)
        later = self.clear(second)
        self.assertGreaterEqual(later.cleared_at, first.cleared_at)
        self.assertEqual(self.settled(), later.cleared_at)

    def test_unclearing_reopens_the_order(self):
        self.clear()
        self.assertIsNotNone(self.settled())
        reopened = cheques.unclear_cheque(self.cheque, self.admin_user)
        self.assertEqual((reopened.status, reopened.cleared_at), ('approved', None))
        self.assertIsNone(self.settled())

    def test_only_an_approved_cheque_can_be_cleared(self):
        pending = cheques.create_cheque(self.order, self.user, {'sayadi_id': OTHER}, [upload(make_image())])
        rejected = self.add_cheque(THIRD, status='rejected')
        for cheque in (pending, rejected):
            with self.subTest(status=cheque.status):
                self.assertEqual(self.errors(cheques.clear_cheque, cheque, self.admin_user)['__all__'], 'فقط چکِ تأییدشده قابل ثبت وصول است.')
        cheques.withdraw_cheque(rejected)
        self.assertIn('تأییدشده', self.errors(cheques.clear_cheque, self.refresh(rejected), self.admin_user)['__all__'])

    def test_clearing_twice_and_unclearing_a_non_cleared_cheque_are_refused(self):
        self.clear()
        self.assertIn('قبلاً', self.errors(cheques.clear_cheque, self.cheque, self.admin_user)['__all__'])
        cheques.unclear_cheque(self.cheque, self.admin_user)
        self.assertIn('وصول‌شده نیست', self.errors(cheques.unclear_cheque, self.cheque, self.admin_user)['__all__'])

    def test_a_canceled_order_cannot_be_cleared_or_reopened(self):
        Order.objects.filter(pk=self.order.pk).update(status='canceled')
        self.assertIn('لغو', self.errors(cheques.clear_cheque, self.cheque, self.admin_user)['__all__'])
        self.assertEqual(self.refresh().status, 'approved')

    def test_clearing_works_after_the_order_is_approved_and_delivered(self):
        from orders.approval import approve_order
        approve_order(self.order, by=self.admin_user)
        Order.objects.filter(pk=self.order.pk).update(status='delivered')
        self.clear()
        self.assertIsNotNone(self.settled())

    def test_a_stale_object_cannot_double_clear(self):
        stale = ChequePayment.objects.get(pk=self.cheque.pk)
        self.clear()
        self.assertIn('قبلاً', self.errors(cheques.clear_cheque, stale, self.admin_user)['__all__'])

    def test_a_cleared_cheque_is_locked_against_review_edit_and_withdrawal(self):
        self.clear()
        self.assertIn('وصول شده', self.errors(cheques.set_review, self.cheque, 'rejected', self.admin_user, 'x')['__all__'])
        self.assertIn('وصول شده', self.errors(cheques.set_review, self.cheque, 'pending_review', self.admin_user)['__all__'])
        self.assertIsNotNone(cheques.edit_blocker(self.refresh()))
        with self.assertRaises(cheques.ChequeError):
            cheques.withdraw_cheque(self.refresh())
        self.assertEqual(self.refresh().status, 'cleared')


class SettlementRefreshTests(ClearingBase):
    def test_a_new_cheque_on_a_settled_order_reopens_it(self):
        self.clear()
        self.assertIsNotNone(self.settled())
        cheques.create_cheque(self.order, self.user, {'sayadi_id': OTHER}, [upload(make_image())])     # هنوز تأیید نشده
        self.assertIsNone(self.settled())

    def test_a_rejected_cheque_blocks_the_settlement(self):
        rejected = self.add_cheque(OTHER, status='rejected')
        self.clear(self.cheque)
        self.assertIsNone(self.settled())
        cheques.withdraw_cheque(rejected)                                                           # حذف چک ردشده: بقیه همه وصول‌شده
        self.assertIsNotNone(self.settled())

    def test_an_order_with_only_withdrawn_cheques_is_never_settled(self):
        order = self.cheque_order()
        cheque = cheques.create_cheque(order, self.user, {'sayadi_id': THIRD}, [upload(make_image())])
        cheques.set_review(cheque, 'rejected', self.admin_user, 'x')
        cheques.withdraw_cheque(self.refresh(cheque))
        self.assertIsNone(self.refresh(order).cheque_settled_at)

    def test_the_order_without_cheques_is_not_settled(self):
        self.assertIsNone(self.refresh(self.cheque_order()).cheque_settled_at)


class CompatibilityTests(ClearingBase):
    def test_a_cleared_cheque_counts_as_active_for_the_deadline(self):
        self.assertIn('cleared', deadline.ACTIVE_STATUSES)
        self.clear()
        Order.objects.filter(pk=self.order.pk).update(cheque_deadline_at=timezone.now() - timedelta(hours=2))
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(deadline.cancel_expired_cheque_orders(), 0)
        self.assertEqual(self.refresh(self.order).status, 'pending')

    def test_a_cleared_cheque_is_not_a_review_blocker(self):
        self.clear()
        self.assertIsNone(cheques.review_blocker(self.refresh(self.order)))
        self.assertIsNone(approval_blocker(self.refresh(self.order)))

    def test_the_cheque_state_treats_cleared_as_approved(self):
        self.clear()
        self.assertEqual(self.refresh(self.order).cheque_state, 'approved')

    def test_the_duplicate_guard_still_covers_cleared_cheques(self):
        self.clear()
        other = self.cheque_order()
        self.assertIn('sayadi_id', self.errors(cheques.create_cheque, other, self.user, {'sayadi_id': VALID}, [upload(make_image())]))

    def test_an_approval_sms_is_not_held_back_by_a_cleared_sibling(self):
        from notifications.models import Notification
        second = cheques.create_cheque(self.order, self.user, {'sayadi_id': OTHER}, [upload(make_image())])
        self.clear(self.cheque)
        with mock.patch('notifications.tasks.deliver_notification.delay'), self.captureOnCommitCallbacks(execute=True):
            cheques.set_review(second, 'approved', self.admin_user)
        self.assertEqual(Notification.objects.filter(template_key='cheque_approved_customer').count(), 1)


# ------------------------------------------------------------------ اعتبار
class CreditReleaseTests(ClearingBase):
    def used(self):
        return credit.outstanding_total(CustomUser.objects.get(pk=self.user.pk))

    def test_a_settled_order_leaves_the_outstanding_credit(self):
        Order.objects.filter(pk=self.order.pk).update(total_price=500_000)
        self.assertEqual(self.used(), 500_000)
        self.clear()
        self.assertEqual(self.used(), 0)

    def test_a_partly_cleared_order_still_counts_in_full(self):
        self.add_cheque(OTHER)
        Order.objects.filter(pk=self.order.pk).update(total_price=500_000)
        self.clear(self.cheque)
        self.assertEqual(self.used(), 500_000)

    def test_unclearing_brings_the_credit_back(self):
        Order.objects.filter(pk=self.order.pk).update(total_price=500_000)
        self.clear()
        cheques.unclear_cheque(self.cheque, self.admin_user)
        self.assertEqual(self.used(), 500_000)

    def test_the_gate_lets_a_new_order_in_after_the_old_one_is_settled(self):
        Order.objects.filter(pk=self.order.pk).update(total_price=500_000, status='delivered')
        set_limit(self.user, 600_000)
        self.assertIsNotNone(credit.credit_block(self.user, 200_000))                 # مانده ۱۰۰٬۰۰۰
        self.clear()
        self.assertIsNone(credit.credit_block(self.user, 200_000))                    # آزاد شد

    def test_other_users_settlement_does_not_affect_me(self):
        mine = make_order(self.user, 300_000)
        other = make_order(self.other, 700_000, cheque_settled_at=timezone.now())
        self.assertEqual(credit.outstanding_total(self.user), self.used())
        self.assertEqual(credit.outstanding_total(self.other), 0)
        self.assertIsNotNone(other.pk and mine.pk)


class ReturnsDeductionTests(ClearingBase):
    def setUp(self):
        super().setUp()
        Order.objects.filter(pk=self.order.pk).update(total_price=1_000_000, status='delivered')
        self.item = OrderItem.objects.get(order=self.order)
        self.reason = ReturnReason.objects.create(title='مغایرت')

    def used(self):
        return credit.outstanding_total(CustomUser.objects.get(pk=self.user.pk))

    def make_return(self, status, item_refund=None, shipping=0, order=None, **extra):
        request = ReturnRequest.objects.create(order=order or self.order, user=self.user, refund_method='wallet', status=status,
                                               shipping_refund_amount=shipping, **extra)
        ReturnItem.objects.create(return_request=request, order_item=self.item, reason=self.reason, requested_quantity=1,
                                  approved_quantity=1 if item_refund is not None else None, refund_amount=item_refund)
        return request

    def test_a_pending_or_approved_return_without_a_final_amount_frees_nothing(self):
        for status in ('PENDING', 'APPROVED', 'ITEM_RECEIVED'):
            self.make_return(status)
        self.assertEqual(self.used(), 1_000_000)

    def test_refund_pending_and_completed_returns_free_their_final_amounts(self):
        self.make_return('REFUND_PENDING', item_refund=200_000, shipping=15_000)
        self.assertEqual(self.used(), 785_000)
        self.make_return('COMPLETED', item_refund=100_000, completed_at=timezone.now())
        self.assertEqual(self.used(), 685_000)

    def test_a_rejected_return_has_no_effect(self):
        request = self.make_return('REJECTED', item_refund=300_000, rejection_reason='x')
        self.assertEqual(self.used(), 1_000_000)
        self.assertEqual(request.status, 'REJECTED')

    def test_a_full_refund_never_makes_the_credit_negative(self):
        self.make_return('COMPLETED', item_refund=1_000_000, shipping=50_000, completed_at=timezone.now())
        self.assertEqual(self.used(), 0)

    def test_returns_of_settled_or_canceled_orders_are_ignored_and_not_double_counted(self):
        live = make_order(self.user, 400_000, status='delivered')
        settled = make_order(self.user, 900_000, status='delivered', cheque_settled_at=timezone.now())
        item = OrderItem.objects.create(order=settled, product=self.item.product, price=100000, quantity=1)
        request = ReturnRequest.objects.create(order=settled, user=self.user, refund_method='wallet', status='COMPLETED',
                                               completed_at=timezone.now())
        ReturnItem.objects.create(return_request=request, order_item=item, reason=self.reason, requested_quantity=1,
                                  approved_quantity=1, refund_amount=500_000)
        self.assertEqual(self.used(), 1_000_000 + 400_000)                         # مرجوعیِ سفارش وصول‌شده از چیزی کم نمی‌کند
        self.assertIsNotNone(live.pk)

    def test_the_gate_uses_the_net_figure(self):
        set_limit(self.user, 1_200_000)
        self.assertIsNotNone(credit.credit_block(self.user, 300_000))               # مانده ۲۰۰٬۰۰۰
        self.make_return('REFUND_PENDING', item_refund=200_000)
        self.assertIsNone(credit.credit_block(self.user, 300_000))                  # مانده ۴۰۰٬۰۰۰

    def test_query_count_is_bounded(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        self.make_return('COMPLETED', item_refund=1, completed_at=timezone.now())
        with CaptureQueriesContext(connection) as captured:
            credit.outstanding_total(self.user)
        self.assertLessEqual(len(captured), 3)
        with CaptureQueriesContext(connection) as captured:
            credit.outstanding_total(self.other)                                     # بدون سفارش درگیر: فقط یک کوئری
        self.assertEqual(len(captured), 1)


# ------------------------------------------------------------------ ادمین و مشتری
class AdminAndCustomerTests(ClearingBase):
    def change_url(self, cheque=None):
        return reverse('admin:orders_chequepayment_change', args=[(cheque or self.cheque).pk])

    def post_action(self, name, *cheques_):
        with self.captureOnCommitCallbacks(execute=True):
            return self.admin.post(reverse('admin:orders_chequepayment_changelist'),
                                   {'action': name, '_selected_action': [c.pk for c in cheques_]}, follow=True)

    def test_the_bulk_action_clears_and_settles(self):
        second = self.add_cheque(OTHER)
        response = self.post_action('clear_cheques', self.cheque, second)
        self.assertContains(response, 'وصول 2 چک ثبت شد')
        self.assertEqual({self.refresh(c).status for c in (self.cheque, second)}, {'cleared'})
        self.assertIsNotNone(self.settled())

    def test_the_bulk_action_skips_unclearable_cheques_with_a_warning(self):
        pending = cheques.create_cheque(self.order, self.user, {'sayadi_id': OTHER}, [upload(make_image())])
        response = self.post_action('clear_cheques', self.cheque, pending)
        self.assertContains(response, 'وصول 1 چک ثبت شد')
        self.assertContains(response, 'فقط چکِ تأییدشده')
        self.assertEqual(self.refresh(pending).status, 'pending_review')
        self.assertIsNone(self.settled())

    def test_the_bulk_unclear_action_reverts(self):
        self.clear()
        response = self.post_action('unclear_cheques', self.cheque)
        self.assertContains(response, 'وصول 1 چک برگردانده شد')
        self.assertEqual(self.refresh().status, 'approved')
        self.assertIsNone(self.settled())

    def test_the_change_page_has_a_secure_clear_button_and_it_works(self):
        page = self.admin.get(self.change_url())
        self.assertContains(page, reverse('admin:orders_chequepayment_clear', args=[self.cheque.pk]))
        self.assertContains(page, 'ثبت وصول چک')
        html = page.content.decode()
        self.assertIn('csrfmiddlewaretoken', html)                                  # توکن از فرم اصلیِ ادمین؛ دکمه فرم تودرتو نیست
        button = html[html.index('ثبت وصول چک') - 300:html.index('ثبت وصول چک')]
        self.assertIn('formaction=', button)
        self.assertIn('formmethod="post"', button)
        self.assertEqual(html.count('<form'), html.count('</form>'))
        self.assertNotRegex(html, r'<form[^>]*>(?:(?!</form>).)*<form')              # هیچ فرمی داخل فرم دیگر نیست
        with self.captureOnCommitCallbacks(execute=True):
            response = self.admin.post(reverse('admin:orders_chequepayment_clear', args=[self.cheque.pk]), follow=True)
        self.assertContains(response, 'وصول چک ثبت شد')
        self.assertEqual(self.refresh().status, 'cleared')
        page = self.admin.get(self.change_url())
        self.assertContains(page, 'بازگرداندن وصول')
        self.assertContains(page, reverse('admin:orders_chequepayment_unclear', args=[self.cheque.pk]))
        self.admin.post(reverse('admin:orders_chequepayment_unclear', args=[self.cheque.pk]))
        self.assertEqual(self.refresh().status, 'approved')

    def test_the_clear_endpoint_is_post_only_and_permission_checked(self):
        url = reverse('admin:orders_chequepayment_clear', args=[self.cheque.pk])
        self.assertEqual(self.admin.get(url).status_code, 405)
        staff = Client()
        staff.force_login(make_approved_user('09120000700', is_staff=True))
        self.assertEqual(staff.post(url).status_code, 403)
        self.assertEqual(Client().post(url).status_code, 302)
        self.assertEqual(self.refresh().status, 'approved')

    def test_a_viewer_cannot_clear(self):
        from django.contrib.auth.models import Permission
        viewer = make_approved_user('09120000701', is_staff=True)
        viewer.user_permissions.add(Permission.objects.get(content_type__app_label='orders', codename='view_chequepayment'))
        client = Client()
        client.force_login(CustomUser.objects.get(pk=viewer.pk))
        self.assertEqual(client.post(reverse('admin:orders_chequepayment_clear', args=[self.cheque.pk])).status_code, 403)
        client.post(reverse('admin:orders_chequepayment_changelist'), {'action': 'clear_cheques', '_selected_action': [self.cheque.pk]})
        self.assertEqual(self.refresh().status, 'approved')

    def test_the_review_form_is_locked_for_a_cleared_cheque(self):
        self.clear()
        page = self.admin.get(self.change_url())
        self.assertNotContains(page, 'name="status"')
        self.admin.post(self.change_url(), {'status': 'rejected', 'rejection_reason': 'x', '_save': '1'})
        self.assertEqual(self.refresh().status, 'cleared')

    def test_the_list_shows_the_cleared_status_and_filter(self):
        self.clear()
        listing = self.admin.get(reverse('admin:orders_chequepayment_changelist'))
        self.assertContains(listing, 'وصول‌شده')
        filtered = self.admin.get(reverse('admin:orders_chequepayment_changelist'), {'status__exact': 'cleared'})
        self.assertContains(filtered, VALID)

    def test_the_order_admin_shows_the_settled_state(self):
        order_url = reverse('admin:orders_order_change', args=[self.order.pk])
        self.assertNotContains(self.admin.get(order_url), 'وصول‌شده')
        self.clear()
        page = self.admin.get(order_url)
        self.assertContains(page, 'وصول‌شده')
        self.assertContains(page, 'زمان وصول کامل چک‌ها')
        self.assertContains(self.admin.get(reverse('admin:orders_order_changelist')), 'وصول‌شده')

    def test_the_customer_sees_the_cleared_cheque_and_the_settlement_line(self):
        detail = reverse('orders:order_detail_full', args=[self.order.id])
        self.assertNotContains(self.client.get(detail), 'data-testid="cheque-settled"')
        self.clear()
        page = self.client.get(detail)
        self.assertContains(page, 'وصول‌شده')
        self.assertContains(page, 'data-testid="cheque-settled"')
        self.assertContains(page, 'تمام چک‌های این سفارش وصول شد')

    def test_the_customer_cheque_list_shows_the_cleared_badge(self):
        self.clear()
        self.assertContains(self.client.get(self.url(self.order)), 'وصول‌شده')

    def test_an_unsettled_partial_state_does_not_show_the_settlement_line(self):
        self.add_cheque(OTHER)
        self.clear(self.cheque)
        self.assertNotContains(self.client.get(reverse('orders:order_detail_full', args=[self.order.id])), 'data-testid="cheque-settled"')
