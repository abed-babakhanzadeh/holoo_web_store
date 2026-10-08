"""
فاز C چکی: بررسی چک توسط مدیر (تأیید / ردِ دارای علت الزامی)، اصلاح و ارسال مجدد توسط مشتری، حذف چکِ ردشده، مانع تأیید سفارش
(بدون چک یا با چک تأییدنشده)، هم‌گامی customer_status با وضعیت واقعی چک، و صفحه‌های ادمین/مشتری.
MEDIA_ROOT برای هر تست یک پوشه‌ی موقت است؛ هیچ فایلی به media/ واقعی نمی‌رود.
"""
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts.testing import make_approved_user
from orders import cheques
from orders.approval import ApprovalError, approval_blocker, approve_order
from orders.history import build_history
from orders.models import ChequeImage, ChequePayment, Order, OrderItem
from orders.tests_cheques import ChequeFlowBase, VALID, make_image, stored_files, upload
from products import stock

OTHER = '6219861099999999'


class ReviewBase(ChequeFlowBase):
    """ سفارش چکی با یک چکِ ثبت‌شده از مسیر واقعی (سرویس) + کاربر ادمین """

    def setUp(self):
        super().setUp()
        self.order = self.cheque_order()
        OrderItem.objects.create(order=self.order, product=self.product, price=100000, quantity=2)
        with stock.transaction.atomic():
            stock.reserve_for_order(self.order.id, {self.product.pk: 2})
        self.cheque = cheques.create_cheque(self.order, self.user, {'sayadi_id': VALID}, [upload(make_image())])
        self.admin_user = make_approved_user('09120000090', is_staff=True, is_superuser=True)
        self.admin = Client()
        self.admin.force_login(self.admin_user)

    def refresh(self, obj=None):
        obj = obj or self.cheque
        return type(obj).objects.get(pk=obj.pk)

    def reject(self, cheque=None, reason='تصویر ناخوانا است'):
        return cheques.set_review(cheque or self.cheque, ChequePayment.STATUS_REJECTED, self.admin_user, reason)

    def approve(self, cheque=None):
        return cheques.set_review(cheque or self.cheque, ChequePayment.STATUS_APPROVED, self.admin_user)

    def errors(self, func, *args, **kwargs):
        with self.assertRaises(cheques.ChequeError) as caught:
            func(*args, **kwargs)
        return caught.exception.errors

    def edit_url(self, cheque=None):
        cheque = cheque or self.cheque
        return reverse('orders:cheque_edit', args=[cheque.order_id, cheque.public_id])

    def withdraw_url(self, cheque=None):
        cheque = cheque or self.cheque
        return reverse('orders:cheque_withdraw', args=[cheque.order_id, cheque.public_id])

    def status_of(self, order=None):
        return self.refresh(order or self.order).customer_status


# ------------------------------------------------------------------ سرویس بررسی
class SetReviewTests(ReviewBase):
    def test_a_new_cheque_waits_for_review(self):
        self.assertEqual(self.cheque.status, ChequePayment.STATUS_PENDING)
        self.assertIsNone(self.cheque.reviewed_at)

    def test_approving_stamps_the_reviewer_and_time(self):
        self.approve()
        cheque = self.refresh()
        self.assertEqual(cheque.status, 'approved')
        self.assertEqual(cheque.reviewed_by, self.admin_user)
        self.assertIsNotNone(cheque.reviewed_at)

    def test_rejecting_requires_a_reason(self):
        for empty in ('', '   ', None, '\n\t'):
            with self.subTest(reason=empty):
                self.assertIn('rejection_reason', self.errors(cheques.set_review, self.cheque, 'rejected', self.admin_user, empty))
        self.assertEqual(self.refresh().status, 'pending_review')

    def test_the_reason_is_normalized_and_length_limited(self):
        self.reject(reason='  تصویر \n\x00 ناخوانا ')
        self.assertEqual(self.refresh().rejection_reason, 'تصویر ناخوانا')
        cheque = cheques.create_cheque(self.order, self.user, {'sayadi_id': OTHER}, [upload(make_image())])
        self.assertIn('rejection_reason', self.errors(cheques.set_review, cheque, 'rejected', self.admin_user, 'x' * 301))

    def test_unknown_and_withdrawn_statuses_cannot_be_set(self):
        self.assertIn('status', self.errors(cheques.set_review, self.cheque, 'withdrawn', self.admin_user))
        self.assertIn('status', self.errors(cheques.set_review, self.cheque, 'bogus', self.admin_user))

    def test_a_withdrawn_cheque_is_not_reviewable(self):
        self.reject()
        cheques.withdraw_cheque(self.cheque)
        errors = self.errors(cheques.set_review, self.cheque, 'approved', self.admin_user)
        self.assertIn('حذف شده', errors['__all__'])

    def test_reviews_are_locked_once_the_order_is_approved_or_canceled(self):
        self.approve()
        approve_order(self.order, by=self.admin_user)
        self.assertIn('تأیید شده', self.errors(cheques.set_review, self.cheque, 'rejected', self.admin_user, 'x')['__all__'])
        other = self.cheque_order()
        cheque = cheques.create_cheque(other, self.user, {'sayadi_id': OTHER}, [upload(make_image())])
        Order.objects.filter(pk=other.pk).update(status='canceled')
        self.assertIn('لغو', self.errors(cheques.set_review, cheque, 'approved', self.admin_user)['__all__'])

    def test_sending_back_to_pending_clears_the_review_stamp(self):
        self.approve()
        cheques.set_review(self.cheque, 'pending_review', self.admin_user)
        cheque = self.refresh()
        self.assertEqual((cheque.status, cheque.reviewed_by, cheque.reviewed_at), ('pending_review', None, None))


# ------------------------------------------------------------------ مانع تأیید سفارش
class ApprovalGateTests(ReviewBase):
    def blocker(self, order=None):
        return approval_blocker(self.refresh(order or self.order))

    def test_a_cheque_order_without_cheques_is_blocked(self):
        order = self.cheque_order()
        OrderItem.objects.create(order=order, product=self.product, price=100000, quantity=1)
        self.assertIn('اطلاعات چک ثبت نشده', self.blocker(order))

    def test_a_pending_cheque_blocks_approval(self):
        self.assertIn('در انتظار بررسی', self.blocker())

    def test_a_rejected_cheque_blocks_approval(self):
        self.reject()
        self.assertIn('ردشده', self.blocker())

    def test_every_cheque_must_be_approved(self):
        second = cheques.create_cheque(self.order, self.user, {'sayadi_id': OTHER}, [upload(make_image())])
        self.approve()
        self.assertIn('در انتظار بررسی', self.blocker())                        # دومی هنوز pending
        self.reject(second)
        self.assertIn('ردشده', self.blocker())                                  # رد از pending جلوتر است
        self.approve(second)
        self.assertIsNone(self.blocker())

    def test_a_withdrawn_cheque_is_ignored(self):
        second = cheques.create_cheque(self.order, self.user, {'sayadi_id': OTHER}, [upload(make_image())])
        self.reject(second)
        cheques.withdraw_cheque(second)
        self.approve()
        self.assertIsNone(self.blocker())

    def test_only_withdrawn_cheques_means_missing(self):
        self.reject()
        cheques.withdraw_cheque(self.cheque)
        self.assertIn('اطلاعات چک ثبت نشده', self.blocker())

    def test_non_cheque_orders_are_not_affected(self):
        cash = self.cheque_order(payment_method='cash', settlement='online')
        OrderItem.objects.create(order=cash, product=self.product, price=100000, quantity=1)
        self.assertIsNone(cheques.review_blocker(cash))
        self.assertIn('پرداخت نشده', approval_blocker(cash))                    # قاعده‌ی قبلی همچنان برقرار

    def test_the_old_messages_keep_priority(self):
        order = self.cheque_order()                                             # بدون ردیف و بدون چک
        self.assertIn('هیچ ردیفی', self.blocker(order))
        Order.objects.filter(pk=self.order.pk).update(status='canceled')
        self.assertIn('لغو', self.blocker())

    def test_approve_order_refuses_until_the_cheque_is_approved(self):
        with self.assertRaises(ApprovalError):
            approve_order(self.order, by=self.admin_user)
        self.assertIsNone(self.refresh(self.order).approved_at)
        self.reject()
        with self.assertRaises(ApprovalError):
            approve_order(self.order, by=self.admin_user)
        cheques.withdraw_cheque(self.cheque)
        with self.assertRaises(ApprovalError):
            approve_order(self.order, by=self.admin_user)
        replacement = cheques.create_cheque(self.order, self.user, {'sayadi_id': OTHER}, [upload(make_image())])
        self.approve(replacement)
        approve_order(self.order, by=self.admin_user)
        self.assertIsNotNone(self.refresh(self.order).approved_at)

    def test_the_admin_approve_action_shows_the_reason(self):
        response = self.admin.post(reverse('admin:orders_order_changelist'),
                                   {'action': 'approve_orders', '_selected_action': [self.order.pk]}, follow=True)
        self.assertContains(response, 'در انتظار بررسی')
        self.assertIsNone(self.refresh(self.order).approved_at)

    def test_the_order_page_blocks_the_approve_button_with_the_reason(self):
        approve_url = reverse('admin:orders_order_approve', args=[self.order.pk])
        page = self.admin.get(reverse('admin:orders_order_change', args=[self.order.id]))
        self.assertContains(page, 'در انتظار بررسی')
        self.assertContains(page, 'تأیید سفارش (مسدود)')
        self.assertNotContains(page, approve_url)
        self.approve()
        page = self.admin.get(reverse('admin:orders_order_change', args=[self.order.id]))
        self.assertNotContains(page, 'تأیید سفارش (مسدود)')
        self.assertContains(page, approve_url)


# ------------------------------------------------------------------ وضعیت مشتری
class CustomerStatusTests(ReviewBase):
    def test_the_status_follows_the_real_cheque_state(self):
        empty = self.cheque_order()
        self.assertEqual(self.status_of(empty), 'awaiting_cheque')
        self.assertEqual(self.refresh(empty).customer_status_display, 'در انتظار ثبت چک')

        self.assertEqual(self.status_of(), 'cheque_under_review')
        self.assertEqual(self.refresh(self.order).customer_status_display, 'در انتظار بررسی چک')

        self.reject()
        self.assertEqual(self.status_of(), 'cheque_needs_correction')
        self.assertEqual(self.refresh(self.order).customer_status_display, 'نیاز به اصلاح اطلاعات چک')

        cheques.withdraw_cheque(self.cheque)
        self.assertEqual(self.status_of(), 'awaiting_cheque')                   # چک حذف‌شده شمرده نمی‌شود

        replacement = cheques.create_cheque(self.order, self.user, {'sayadi_id': OTHER}, [upload(make_image())])
        self.assertEqual(self.status_of(), 'cheque_under_review')
        self.approve(replacement)
        self.assertEqual(self.status_of(), 'cheque_approved')
        self.assertEqual(self.refresh(self.order).customer_status_display, 'چک تأیید شد')

        approve_order(self.order, by=self.admin_user)
        self.assertEqual(self.status_of(), 'processing')

    def test_a_rejected_cheque_outweighs_a_pending_one(self):
        second = cheques.create_cheque(self.order, self.user, {'sayadi_id': OTHER}, [upload(make_image())])
        self.reject(second)
        self.assertEqual(self.status_of(), 'cheque_needs_correction')

    def test_a_canceled_order_is_canceled_whatever_the_cheque(self):
        Order.objects.filter(pk=self.order.pk).update(status='canceled')
        self.assertEqual(self.status_of(), 'canceled')

    def test_the_history_page_uses_annotations_without_n_plus_one(self):
        orders = [self.order]
        for n in range(5):
            other = self.cheque_order()
            cheques.create_cheque(other, self.user, {'sayadi_id': f'62198610000000{n:02d}'}, [upload(make_image())])
            orders.append(other)
        self.reject(ChequePayment.objects.get(order=orders[1]))

        def run():
            return build_history(self.user, {})
        run()                                                                   # گرم‌کردن (کش/سایت‌ستینگ)
        small = len(self._queries(run))
        for n in range(5, 9):
            other = self.cheque_order()
            cheques.create_cheque(other, self.user, {'sayadi_id': f'62198610000000{n:02d}'}, [upload(make_image())])
        self.assertEqual(len(self._queries(run)), small)                        # تعداد کوئری با تعداد سفارش‌ها رشد نمی‌کند

    def _queries(self, func):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as captured:
            func()
        return captured.captured_queries

    def test_the_history_tabs_show_the_new_states(self):
        self.reject()
        response = self.client.get(reverse('orders:order_history'))
        self.assertContains(response, 'نیاز به اصلاح اطلاعات چک')


# ------------------------------------------------------------------ اصلاح توسط مشتری
class EditChequeTests(ReviewBase):
    def setUp(self):
        super().setUp()
        self.reject()

    def edit(self, *, sayadi=VALID, files=None, remove=(), client=None, **extra):
        data = {'sayadi_id': sayadi, 'images': files if files is not None else [], 'remove_images': list(remove)}
        data.update(extra)
        return (client or self.client).post(self.edit_url(), data)

    def test_only_a_rejected_cheque_is_editable(self):
        for status in ('pending_review', 'approved'):
            with self.subTest(status=status):
                ChequePayment.objects.filter(pk=self.cheque.pk).update(status=status)
                response = self.client.get(self.edit_url())
                self.assertRedirects(response, self.url(self.order), fetch_redirect_response=False)
                self.assertEqual(self.edit(sayadi=OTHER).status_code, 302)
                self.assertEqual(self.refresh().sayadi_id, VALID)

    def test_the_edit_page_shows_the_reason_and_the_current_values(self):
        response = self.client.get(self.edit_url())
        self.assertContains(response, 'تصویر ناخوانا است')
        self.assertContains(response, f'value="{VALID}"')
        self.assertContains(response, 'name="remove_images"')

    def test_editing_resubmits_the_cheque_for_review(self):
        response = self.edit(sayadi=OTHER, amount='2,000,000', bank_name='ملی')
        self.assertRedirects(response, self.url(self.order), fetch_redirect_response=False)
        cheque = self.refresh()
        self.assertEqual((cheque.status, cheque.sayadi_id, int(cheque.amount), cheque.bank_name), ('pending_review', OTHER, 2000000, 'ملی'))
        self.assertIsNone(cheque.reviewed_at)
        self.assertEqual(self.status_of(), 'cheque_under_review')
        self.assertIn('در انتظار بررسی', approval_blocker(self.refresh(self.order)))

    def test_images_can_be_kept_removed_and_added(self):
        old = self.cheque.images.get()
        self.edit(files=[upload(make_image('PNG'), 'a.png', 'image/png')])      # نگه داشتن قدیمی + افزودن یکی
        self.assertEqual(self.refresh().images.count(), 2)
        self.reject()
        self.edit(remove=[old.public_id])                                       # حذف قدیمی؛ تازه می‌ماند
        images = list(self.refresh().images.all())
        self.assertEqual(len(images), 1)
        self.assertNotEqual(images[0].pk, old.pk)

    def test_removed_files_are_deleted_from_disk(self):
        old = self.cheque.images.get()
        before = set(stored_files(self.media_root))
        with self.captureOnCommitCallbacks(execute=True):
            self.edit(remove=[old.public_id], files=[upload(make_image('WEBP'), 'a.webp', 'image/webp')])
        after = set(stored_files(self.media_root))
        self.assertEqual(len(after), 1)
        self.assertTrue(before.isdisjoint(after))

    def test_at_least_one_and_at_most_five_images_remain(self):
        old = self.cheque.images.get()
        response = self.edit(remove=[old.public_id])
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'حداقل یک تصویر', status_code=400)
        response = self.edit(files=[upload(make_image()) for _ in range(5)])    # ۱ نگه‌داشته + ۵ = ۶
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.refresh().status, 'rejected')
        self.assertEqual(self.refresh().images.count(), 1)
        response = self.edit(files=[upload(make_image()) for _ in range(4)])    # ۱ + ۴ = ۵ مجاز
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.refresh().images.count(), 5)

    def test_bad_input_changes_nothing_and_leaves_no_files(self):
        before = set(stored_files(self.media_root))
        response = self.edit(sayadi='123', files=[upload(b'%PDF-1.4 fake', 'x.pdf', 'application/pdf')])
        self.assertEqual(response.status_code, 400)
        cheque = self.refresh()
        self.assertEqual((cheque.status, cheque.sayadi_id), ('rejected', VALID))
        self.assertEqual(set(stored_files(self.media_root)), before)

    def test_a_duplicate_sayadi_id_is_refused_but_the_same_id_is_fine(self):
        other_order = self.cheque_order()
        cheques.create_cheque(other_order, self.user, {'sayadi_id': OTHER}, [upload(make_image())])
        response = self.edit(sayadi=OTHER)
        self.assertContains(response, 'قبلاً در سامانه ثبت شده', status_code=400)
        self.assertEqual(self.edit(sayadi=VALID).status_code, 302)              # همان شناسه‌ی خودش مشکلی ندارد

    def test_ownership_and_login_are_enforced(self):
        stranger = Client()
        stranger.force_login(make_approved_user('09120000097'))
        self.assertEqual(stranger.get(self.edit_url()).status_code, 404)
        self.assertEqual(self.edit(sayadi=OTHER, client=stranger).status_code, 404)
        self.assertEqual(stranger.post(self.withdraw_url()).status_code, 404)
        self.assertEqual(Client().get(self.edit_url()).status_code, 302)
        self.assertEqual(self.refresh().sayadi_id, VALID)
        self.assertEqual(self.refresh().status, 'rejected')

    def test_a_mismatched_order_id_is_a_404(self):
        other = self.cheque_order()
        url = reverse('orders:cheque_edit', args=[other.id, self.cheque.public_id])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_editing_is_blocked_after_the_order_is_approved_or_canceled(self):
        for fields in ({'approved_at': timezone.now()}, {'status': 'canceled'}):
            with self.subTest(fields=fields):
                Order.objects.filter(pk=self.order.pk).update(**fields)
                self.assertEqual(self.edit(sayadi=OTHER).status_code, 302)
                self.assertEqual(self.refresh().sayadi_id, VALID)
                self.assertEqual(self.refresh().status, 'rejected')

    def test_a_stale_form_after_the_admin_changed_the_status_does_not_overwrite(self):
        self.approve()                                                           # مدیر وسط کار تأیید کرد
        self.edit(sayadi=OTHER)
        cheque = self.refresh()
        self.assertEqual((cheque.status, cheque.sayadi_id), ('approved', VALID))

    def test_the_edit_view_does_not_serve_withdrawn_cheques(self):
        cheques.withdraw_cheque(self.cheque)
        self.assertEqual(self.client.get(self.edit_url()).status_code, 404)

    def test_the_rate_limit_applies_to_edits(self):
        from unittest import mock
        with mock.patch('orders.cheques._rate_limited', return_value=True):
            response = self.edit(sayadi=OTHER)
        self.assertEqual(response.status_code, 429)


# ------------------------------------------------------------------ حذف توسط مشتری
class WithdrawChequeTests(ReviewBase):
    def test_only_a_rejected_cheque_can_be_withdrawn(self):
        response = self.client.post(self.withdraw_url())
        self.assertRedirects(response, self.url(self.order), fetch_redirect_response=False)
        self.assertEqual(self.refresh().status, 'pending_review')               # در حال بررسی: حذف نمی‌شود
        self.reject()
        self.client.post(self.withdraw_url())
        self.assertEqual(self.refresh().status, 'withdrawn')

    def test_get_is_not_allowed(self):
        self.reject()
        self.assertEqual(self.client.get(self.withdraw_url()).status_code, 405)
        self.assertEqual(self.refresh().status, 'rejected')

    def test_the_row_is_kept_for_history_and_the_sayadi_id_is_freed(self):
        self.reject()
        self.client.post(self.withdraw_url())
        self.assertTrue(ChequePayment.objects.filter(pk=self.cheque.pk).exists())
        other = self.cheque_order()
        cheques.create_cheque(other, self.user, {'sayadi_id': VALID}, [upload(make_image())])    # شناسه‌ی آزاد شده

    def test_withdrawn_cheques_do_not_count_toward_the_cap(self):
        for n in range(cheques.MAX_CHEQUES_PER_ORDER - 1):
            cheques.create_cheque(self.order, self.user, {'sayadi_id': f'62198610111111{n:02d}'}, [upload(make_image())])
        self.assertIsNotNone(cheques.submission_blocker(self.order))
        self.reject()
        cheques.withdraw_cheque(self.cheque)
        self.assertIsNone(cheques.submission_blocker(self.refresh(self.order)))

    def test_withdrawn_cheques_are_hidden_from_the_customer_pages(self):
        self.reject()
        self.client.post(self.withdraw_url())
        page = self.client.get(self.url(self.order))
        self.assertNotContains(page, VALID)
        detail = self.client.get(reverse('orders:order_detail_full', args=[self.order.id]))
        self.assertNotContains(detail, VALID)


# ------------------------------------------------------------------ صفحه‌های مشتری
class CustomerPagesTests(ReviewBase):
    def test_the_list_shows_the_reason_and_the_actions_only_for_a_rejected_cheque(self):
        page = self.client.get(self.url(self.order))
        self.assertNotContains(page, 'علت رد')
        self.reject(reason='سررسید مشخص نیست')
        page = self.client.get(self.url(self.order))
        self.assertContains(page, 'سررسید مشخص نیست')
        self.assertContains(page, self.edit_url())
        self.assertContains(page, self.withdraw_url())

    def test_no_actions_once_the_order_is_closed(self):
        self.reject()
        Order.objects.filter(pk=self.order.pk).update(status='canceled')
        page = self.client.get(self.url(self.order))
        self.assertNotContains(page, self.edit_url())
        self.assertNotContains(page, self.withdraw_url())

    def test_the_order_detail_shows_the_reason_and_the_edit_link(self):
        self.reject(reason='تصویر ناخوانا است')
        detail = self.client.get(reverse('orders:order_detail_full', args=[self.order.id]))
        self.assertContains(detail, 'تصویر ناخوانا است')
        self.assertContains(detail, self.edit_url())
        self.assertContains(detail, 'نیاز به اصلاح')

    def test_a_cheque_reason_is_escaped(self):
        self.reject(reason='<script>alert(1)</script>')
        page = self.client.get(self.url(self.order))
        self.assertNotContains(page, '<script>alert(1)</script>')
        self.assertContains(page, '&lt;script&gt;')

    def test_the_progress_bar_mirrors_the_cheque_state(self):
        detail = self.client.get(reverse('orders:order_detail_full', args=[self.order.id]))
        self.assertContains(detail, 'در انتظار بررسی چک')
        self.reject()
        detail = self.client.get(reverse('orders:order_detail_full', args=[self.order.id]))
        self.assertContains(detail, 'نیاز به اصلاح اطلاعات چک')


# ------------------------------------------------------------------ ادمین
class AdminReviewTests(ReviewBase):
    def change_url(self, cheque=None):
        return reverse('admin:orders_chequepayment_change', args=[(cheque or self.cheque).pk])

    def changelist(self):
        return reverse('admin:orders_chequepayment_changelist')

    def post_form(self, status, reason='', cheque=None):
        return self.admin.post(self.change_url(cheque), {'status': status, 'rejection_reason': reason})

    def test_the_change_page_renders_with_the_review_fields(self):
        page = self.admin.get(self.change_url())
        self.assertContains(page, 'name="status"')
        self.assertContains(page, 'name="rejection_reason"')
        self.assertNotContains(page, 'value="withdrawn"')

    def test_approving_through_the_form(self):
        self.post_form('approved')
        cheque = self.refresh()
        self.assertEqual((cheque.status, cheque.reviewed_by), ('approved', self.admin_user))

    def test_rejecting_through_the_form_requires_a_reason(self):
        response = self.post_form('rejected', '')
        self.assertEqual(response.status_code, 200)                              # فرم دوباره با خطا
        self.assertContains(response, 'علت رد')
        self.assertEqual(self.refresh().status, 'pending_review')
        self.post_form('rejected', 'مبلغ با فاکتور نمی‌خواند')
        cheque = self.refresh()
        self.assertEqual((cheque.status, cheque.rejection_reason), ('rejected', 'مبلغ با فاکتور نمی‌خواند'))

    def test_the_cheque_data_cannot_be_changed_from_the_admin(self):
        self.admin.post(self.change_url(), {'status': 'approved', 'sayadi_id': OTHER, 'amount': '5', 'bank_name': 'x'})
        cheque = self.refresh()
        self.assertEqual((cheque.sayadi_id, cheque.amount, cheque.bank_name), (VALID, None, ''))

    def test_the_bulk_approve_action(self):
        second = cheques.create_cheque(self.order, self.user, {'sayadi_id': OTHER}, [upload(make_image())])
        self.admin.post(self.changelist(), {'action': 'approve_cheques', '_selected_action': [self.cheque.pk, second.pk]})
        self.assertEqual(ChequePayment.objects.filter(status='approved').count(), 2)
        self.assertIsNone(approval_blocker(self.refresh(self.order)))

    def test_the_bulk_reject_action_goes_through_the_reason_page(self):
        data = {'action': 'reject_cheques', '_selected_action': [self.cheque.pk]}
        page = self.admin.post(self.changelist(), data)
        self.assertContains(page, 'name="reason"')
        self.assertEqual(self.refresh().status, 'pending_review')               # هنوز تغییری نکرده
        empty = self.admin.post(self.changelist(), {**data, 'apply': '1', 'reason': '  '})
        self.assertContains(empty, 'علت رد را بنویسید')
        self.assertEqual(self.refresh().status, 'pending_review')
        self.admin.post(self.changelist(), {**data, 'apply': '1', 'reason': 'ناخوانا'})
        cheque = self.refresh()
        self.assertEqual((cheque.status, cheque.rejection_reason), ('rejected', 'ناخوانا'))

    def test_the_actions_skip_withdrawn_and_locked_cheques_with_a_warning(self):
        self.reject()
        cheques.withdraw_cheque(self.cheque)
        response = self.admin.post(self.changelist(), {'action': 'approve_cheques', '_selected_action': [self.cheque.pk]}, follow=True)
        self.assertContains(response, 'حذف شده')
        self.assertEqual(self.refresh().status, 'withdrawn')

    def test_a_locked_order_makes_the_review_fields_read_only(self):
        self.approve()
        approve_order(self.order, by=self.admin_user)
        page = self.admin.get(self.change_url())
        self.assertNotContains(page, 'name="rejection_reason"')
        self.post_form('rejected', 'دیر شد')
        self.assertEqual(self.refresh().status, 'approved')

    def test_review_needs_the_change_permission(self):
        User = get_user_model()
        staff = make_approved_user('09120000091', is_staff=True)
        client = Client()
        client.force_login(staff)
        self.assertEqual(client.get(self.change_url()).status_code, 403)
        self.assertEqual(client.post(self.change_url(), {'status': 'approved'}).status_code, 403)
        client.post(self.changelist(), {'action': 'approve_cheques', '_selected_action': [self.cheque.pk]})
        self.assertEqual(self.refresh().status, 'pending_review')
        self.assertTrue(User.objects.filter(pk=staff.pk).exists())

    def test_the_order_list_shows_the_cheque_state_column(self):
        page = self.admin.get(reverse('admin:orders_order_changelist'))
        self.assertContains(page, 'در انتظار بررسی')
        self.reject()
        page = self.admin.get(reverse('admin:orders_order_changelist'))
        self.assertContains(page, 'نیاز به اصلاح')

    def test_the_order_page_links_each_cheque_to_its_review(self):
        page = self.admin.get(reverse('admin:orders_order_change', args=[self.order.id]))
        self.assertContains(page, self.change_url())

    def test_add_and_delete_stay_forbidden(self):
        self.assertEqual(self.admin.get(reverse('admin:orders_chequepayment_add')).status_code, 403)
        self.assertEqual(self.admin.post(reverse('admin:orders_chequepayment_delete', args=[self.cheque.pk]), {'post': 'yes'}).status_code, 403)

    def test_tests_never_touch_the_real_media_folder(self):
        from django.conf import settings
        self.assertTrue(str(settings.MEDIA_ROOT).startswith(str(self.media_root)))
        self.assertEqual(len(stored_files(self.media_root)), ChequeImage.objects.count())
