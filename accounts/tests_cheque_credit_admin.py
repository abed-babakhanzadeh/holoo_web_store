"""
فاز F3 درخواست خرید چکی: پنل ادمین — لیست/فیلتر/جستجو (با ماسک کدملی)، صفحه‌ی بررسی فقط‌خواندنی، نمایش امن مدارک (nosniff + CSP sandbox)،
فرم تصمیم (تأیید/رد با علت الزامی)، اکشن‌های گروهی با صفحه‌ی میانی، کنترل دسترسی و پیوند در صفحه‌ی کاربر.
MEDIA_ROOT هر تست موقت است؛ هیچ فایلی به media/ واقعی نمی‌رود.
"""
import io
import uuid
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import Permission
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from accounts import cheque_credit_service as service
from accounts.cheque_credit import ChequeCreditRequest
from accounts.models import CustomUser
from accounts.signals import cheque_credit_approved, cheque_credit_rejected
from accounts.tests_cheque_credit import IBAN, CreditBase, docs, form_data, make_user


def staff_with(*codenames, phone):
    user = make_user(phone, is_staff=True)
    for codename in codenames:
        user.user_permissions.add(Permission.objects.get(content_type__app_label='accounts', codename=codename))
    return CustomUser.objects.get(pk=user.pk)


class AdminBase(CreditBase):
    def setUp(self):
        super().setUp()
        self.admin_client = Client()
        self.admin_client.force_login(self.admin)
        self.changelist = reverse('admin:accounts_chequecreditrequest_changelist')
        self.request = self.submit(files=docs('cheque_book', 'national_card'))

    def change_url(self, request=None):
        return reverse('admin:accounts_chequecreditrequest_change', args=[(request or self.request).pk])

    def client_for(self, user):
        client = Client()
        client.force_login(user)
        return client

    def decide(self, **data):
        payload = {'status': 'pending', 'rejection_reason': '', 'approved_limit': '', 'admin_note': '', '_save': 'Save'}
        payload.update(data)
        with self.captureOnCommitCallbacks(execute=True):
            return self.admin_client.post(self.change_url(), payload, follow=True)


# ------------------------------------------------------------------ دسترسی
class AccessTests(AdminBase):
    def test_anonymous_is_sent_to_login(self):
        anonymous = Client()
        document = self.request.documents.first()
        for url in (self.changelist, self.change_url(), reverse('admin:accounts_chequecreditrequest_document', args=[document.public_id])):
            with self.subTest(url=url):
                self.assertEqual(anonymous.get(url).status_code, 302)

    def test_plain_staff_without_permissions_gets_403_everywhere(self):
        staff = self.client_for(make_user('09120000901', is_staff=True))
        document = self.request.documents.first()
        for url in (self.changelist, self.change_url(), reverse('admin:accounts_chequecreditrequest_document', args=[document.public_id])):
            with self.subTest(url=url):
                self.assertEqual(staff.get(url).status_code, 403)
        self.assertEqual(staff.post(self.change_url(), {'status': 'approved'}).status_code, 403)

    def test_a_viewer_can_look_but_not_decide(self):
        viewer = self.client_for(staff_with('view_chequecreditrequest', phone='09120000902'))
        page = viewer.get(self.change_url())
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'name="rejection_reason"')
        self.assertNotContains(page, 'name="_save"')
        self.assertEqual(viewer.post(self.change_url(), {'status': 'approved', '_save': '1'}).status_code, 403)
        self.assertEqual(self.reload(self.request).status, 'pending')
        self.assertFalse(self.reload(self.user).can_purchase_with_check)

    def test_a_viewer_cannot_run_the_actions(self):
        viewer = self.client_for(staff_with('view_chequecreditrequest', phone='09120000903'))
        viewer.post(self.changelist, {'action': 'approve_requests', '_selected_action': [self.request.pk]})
        viewer.post(self.changelist, {'action': 'reject_requests', '_selected_action': [self.request.pk], 'apply': '1', 'reason': 'x'})
        self.assertEqual(self.reload(self.request).status, 'pending')

    def test_a_reviewer_with_change_permission_can_decide(self):
        reviewer = staff_with('view_chequecreditrequest', 'change_chequecreditrequest', phone='09120000904')
        client = self.client_for(reviewer)
        with self.captureOnCommitCallbacks(execute=True):
            client.post(self.change_url(), {'status': 'approved', 'rejection_reason': '', 'approved_limit': '50000000', 'admin_note': '', '_save': '1'})
        decided = self.reload(self.request)
        self.assertEqual((decided.status, decided.reviewed_by), ('approved', reviewer))

    def test_add_and_delete_are_forbidden_even_for_superusers(self):
        self.assertEqual(self.admin_client.get(reverse('admin:accounts_chequecreditrequest_add')).status_code, 403)
        self.assertEqual(self.admin_client.post(reverse('admin:accounts_chequecreditrequest_delete', args=[self.request.pk]),
                                                {'post': 'yes'}).status_code, 403)
        self.assertTrue(ChequeCreditRequest.objects.filter(pk=self.request.pk).exists())


# ------------------------------------------------------------------ لیست
class ChangelistTests(AdminBase):
    def test_the_list_shows_the_columns_with_a_masked_national_code(self):
        page = self.admin_client.get(self.changelist)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, self.user.phone_number)
        self.assertContains(page, 'علی رضایی')
        self.assertContains(page, 'فروشگاه نمونه')
        self.assertContains(page, 'در انتظار بررسی')
        self.assertContains(page, f'{self.user.national_code[:3]}*****{self.user.national_code[-2:]}')
        self.assertNotContains(page, self.user.national_code)                      # کد کامل در لیست نیست

    def test_the_detail_page_shows_the_full_national_code(self):
        self.assertContains(self.admin_client.get(self.change_url()), self.user.national_code)

    def test_the_status_filter_and_date_filter_work(self):
        other = make_user('09120000910', price_level=2)
        second = self.submit(user=other)
        service.reject_request(second, self.admin, 'ناخوانا')
        pending = self.admin_client.get(self.changelist, {'status': 'pending'})
        self.assertContains(pending, self.user.phone_number)
        self.assertNotContains(pending, other.phone_number)
        rejected = self.admin_client.get(self.changelist, {'status': 'rejected'})
        self.assertContains(rejected, other.phone_number)
        self.assertNotContains(rejected, self.user.phone_number)
        self.assertContains(self.admin_client.get(self.changelist), 'created_at__gte')          # فیلتر تاریخ

    def test_search_by_phone_name_national_code_and_business(self):
        other = make_user('09120000911', price_level=2, first_name='مریم', last_name='کاظمی')
        self.submit(user=other, data=form_data(business_name='فروشگاه خاص'))
        for term, expected, absent in (
                (self.user.phone_number, self.user.phone_number, other.phone_number),
                ('مریم', other.phone_number, self.user.phone_number),
                ('کاظمی', other.phone_number, self.user.phone_number),
                (other.national_code, other.phone_number, self.user.phone_number),
                ('خاص', other.phone_number, self.user.phone_number)):
            with self.subTest(term=term):
                page = self.admin_client.get(self.changelist, {'q': term})
                self.assertContains(page, expected)
                self.assertNotContains(page, absent)

    def test_the_list_does_not_grow_in_queries_with_the_rows(self):
        def count():
            with CaptureQueriesContext(connection) as captured:
                self.admin_client.get(self.changelist)
            return len(captured)
        count()
        small = count()
        for n in range(4):
            self.submit(user=make_user(f'0912000092{n}', price_level=2))
        self.assertEqual(count(), small)

    def test_text_is_escaped(self):
        other = make_user('09120000915', price_level=2)
        self.submit(user=other, data=form_data(business_name='<script>alert(1)</script>'))
        page = self.admin_client.get(self.changelist)
        self.assertNotContains(page, '<script>alert(1)</script>')
        self.assertContains(page, '&lt;script&gt;')


# ------------------------------------------------------------------ صفحه‌ی بررسی و مدارک
class DetailAndDocumentTests(AdminBase):
    def test_the_data_is_read_only_and_the_decision_form_is_present(self):
        page = self.admin_client.get(self.change_url())
        for name in ('status', 'rejection_reason', 'approved_limit', 'admin_note'):
            self.assertContains(page, f'name="{name}"')
        for name in ('first_name', 'national_code', 'iban', 'business_name', 'requested_limit'):
            self.assertNotContains(page, f'name="{name}"')
        self.assertContains(page, IBAN)
        self.assertNotContains(page, '<option value="canceled"')                   # ادمین انصراف نمی‌دهد

    def test_the_thumbnails_use_the_secure_admin_viewer_not_a_public_path(self):
        page = self.admin_client.get(self.change_url())
        for doc in self.request.documents.all():
            self.assertContains(page, reverse('admin:accounts_chequecreditrequest_document', args=[doc.public_id]))
        self.assertNotContains(page, '/media/')
        self.assertContains(page, 'تصویر دسته‌چک')
        self.assertContains(page, 'کارت ملی')

    def test_the_document_view_serves_hardened_images(self):
        doc = self.request.documents.first()
        response = self.admin_client.get(reverse('admin:accounts_chequecreditrequest_document', args=[doc.public_id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/jpeg')
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertIn('sandbox', response['Content-Security-Policy'])
        self.assertIn("default-src 'none'", response['Content-Security-Policy'])
        self.assertTrue(response['Content-Disposition'].startswith('inline'))
        self.assertIn('no-store', response['Cache-Control'])
        Image.open(io.BytesIO(b''.join(response.streaming_content))).verify()

    def test_unknown_or_purged_documents_are_404(self):
        self.assertEqual(self.admin_client.get(reverse('admin:accounts_chequecreditrequest_document', args=[uuid.uuid4()])).status_code, 404)
        doc = self.request.documents.first()
        service.reject_request(self.request, self.admin, 'x')
        ChequeCreditRequest.objects.filter(pk=self.request.pk).update(decided_at=timezone.now() - timedelta(days=90))
        with self.captureOnCommitCallbacks(execute=True):
            service.purge_expired_documents()
        self.assertEqual(self.admin_client.get(reverse('admin:accounts_chequecreditrequest_document', args=[doc.public_id])).status_code, 404)
        page = self.admin_client.get(self.change_url())
        self.assertContains(page, 'پاک شده‌اند')

    def test_the_page_shows_the_users_current_permission_state(self):
        self.assertContains(self.admin_client.get(self.change_url()), 'غیرفعال')
        service.approve_request(self.request, self.admin)
        self.assertContains(self.admin_client.get(self.change_url()), 'فعال')

    def test_a_decided_request_is_fully_read_only(self):
        service.reject_request(self.request, self.admin, 'مدارک ناقص')
        page = self.admin_client.get(self.change_url())
        self.assertNotContains(page, 'name="rejection_reason"')
        self.assertNotContains(page, 'name="status"')
        self.assertContains(page, 'مدارک ناقص')


# ------------------------------------------------------------------ تصمیم از فرم
class DecisionFormTests(AdminBase):
    def test_approval_turns_the_permission_on_and_records_everything(self):
        seen = []
        handler = lambda sender, request, **kw: seen.append(request.pk)                         # noqa: E731
        cheque_credit_approved.connect(handler, weak=False, dispatch_uid='admin_approved')
        self.addCleanup(cheque_credit_approved.disconnect, dispatch_uid='admin_approved')
        response = self.decide(status='approved', approved_limit='30000000', admin_note='سابقه‌ی خوب')
        self.assertContains(response, 'مجوز خرید چکی برای کاربر فعال شد')
        decided = self.reload(self.request)
        self.assertEqual((decided.status, int(decided.approved_limit), decided.admin_note, decided.reviewed_by),
                         ('approved', 30000000, 'سابقه‌ی خوب', self.admin))
        self.assertIsNotNone(decided.reviewed_at)
        self.assertTrue(self.reload(self.user).can_purchase_with_check)
        self.assertEqual(seen, [self.request.pk])

    def test_the_limit_is_required_for_approval_and_prefilled_with_the_requested_limit(self):
        page = self.admin_client.get(self.change_url())
        self.assertContains(page, 'value="50000000"')                               # پیش‌فرض: سقف درخواستی مشتری
        response = self.decide(status='approved', approved_limit='')
        self.assertContains(response, 'سقف اعتبار را مشخص کنید')
        self.assertEqual(self.reload(self.request).status, 'pending')
        self.assertFalse(self.reload(self.user).can_purchase_with_check)

    def test_approval_writes_the_limit_to_the_user(self):
        self.decide(status='approved', approved_limit='20000000')
        self.assertEqual(int(self.reload(self.user).cheque_credit_limit), 20000000)
        self.assertEqual(int(self.reload(self.request).approved_limit), 20000000)

    def test_a_non_positive_limit_is_a_form_error(self):
        response = self.decide(status='approved', approved_limit='0')
        self.assertContains(response, 'بیشتر از صفر')
        self.assertEqual(self.reload(self.request).status, 'pending')

    def test_rejection_requires_a_reason(self):
        response = self.decide(status='rejected', rejection_reason='')
        self.assertContains(response, 'علت رد را بنویسید')
        self.assertEqual(self.reload(self.request).status, 'pending')
        self.assertNotContains(response, '(ردشده)')                                 # عنوان صفحه وضعیت ذخیره‌نشده را نشان نمی‌دهد
        self.assertContains(response, 'name="rejection_reason"')                    # فرم همچنان قابل ویرایش است
        seen = []
        handler = lambda sender, request, **kw: seen.append(request.rejection_reason)           # noqa: E731
        cheque_credit_rejected.connect(handler, weak=False, dispatch_uid='admin_rejected')
        self.addCleanup(cheque_credit_rejected.disconnect, dispatch_uid='admin_rejected')
        self.decide(status='rejected', rejection_reason='تصویر دسته‌چک ناخوانا است', admin_note='تماس گرفته شد')
        decided = self.reload(self.request)
        self.assertEqual((decided.status, decided.rejection_reason, decided.admin_note), ('rejected', 'تصویر دسته‌چک ناخوانا است', 'تماس گرفته شد'))
        self.assertFalse(self.reload(self.user).can_purchase_with_check)
        self.assertEqual(seen, ['تصویر دسته‌چک ناخوانا است'])

    def test_a_note_alone_keeps_the_request_pending(self):
        self.decide(status='pending', admin_note='منتظر تماس مشتری')
        saved = self.reload(self.request)
        self.assertEqual((saved.status, saved.admin_note), ('pending', 'منتظر تماس مشتری'))
        self.assertFalse(self.reload(self.user).can_purchase_with_check)

    def test_a_decision_is_final(self):
        self.decide(status='rejected', rejection_reason='x')
        self.decide(status='approved', approved_limit='50000000')
        self.assertEqual(self.reload(self.request).status, 'rejected')
        self.assertFalse(self.reload(self.user).can_purchase_with_check)

    def test_a_stale_form_cannot_override_a_decision_made_elsewhere(self):
        service.cancel_request(self.request, self.user)                              # مشتری بین باز کردن فرم و ذخیره انصراف داد
        response = self.decide(status='approved', approved_limit='50000000')
        self.assertEqual(self.reload(self.request).status, 'canceled')
        self.assertFalse(self.reload(self.user).can_purchase_with_check)
        self.assertEqual(response.status_code, 200)

    def test_the_approval_is_atomic_with_the_permission(self):
        with mock.patch.object(CustomUser.objects.__class__, 'filter', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                self.decide(status='approved', approved_limit='50000000')
        self.assertEqual(self.reload(self.request).status, 'pending')
        self.assertFalse(self.reload(self.user).can_purchase_with_check)

    def test_the_reason_is_escaped_on_the_page(self):
        service.reject_request(self.request, self.admin, '<b>x</b>')
        page = self.admin_client.get(self.change_url())
        self.assertNotContains(page, '<b>x</b>')
        self.assertContains(page, '&lt;b&gt;x&lt;/b&gt;')


# ------------------------------------------------------------------ اکشن‌ها
class ActionTests(AdminBase):
    def setUp(self):
        super().setUp()
        self.other = self.submit(user=make_user('09120000930', price_level=2))

    def run_action(self, name, ids, **extra):
        data = {'action': name, '_selected_action': ids, **extra}
        with self.captureOnCommitCallbacks(execute=True):
            return self.admin_client.post(self.changelist, data, follow=True)

    def test_bulk_approve_activates_every_selected_user(self):
        response = self.run_action('approve_requests', [self.request.pk, self.other.pk])
        self.assertContains(response, '2 درخواست تأیید')
        self.assertEqual(sorted(ChequeCreditRequest.objects.values_list('status', flat=True)), ['approved', 'approved'])
        self.assertTrue(self.reload(self.user).can_purchase_with_check)
        self.assertTrue(self.reload(self.other.user).can_purchase_with_check)

    def test_bulk_approve_skips_decided_ones_with_a_warning(self):
        service.reject_request(self.other, self.admin, 'x')
        response = self.run_action('approve_requests', [self.request.pk, self.other.pk])
        self.assertContains(response, '1 درخواست تأیید')
        self.assertContains(response, 'قبلاً')
        self.assertEqual(self.reload(self.other).status, 'rejected')
        self.assertFalse(self.reload(self.other.user).can_purchase_with_check)

    def test_bulk_reject_goes_through_the_reason_page(self):
        page = self.run_action('reject_requests', [self.request.pk, self.other.pk])
        self.assertContains(page, 'name="reason"')
        self.assertContains(page, self.user.phone_number)
        self.assertEqual(ChequeCreditRequest.objects.filter(status='pending').count(), 2)      # هنوز تغییری نکرده
        empty = self.run_action('reject_requests', [self.request.pk, self.other.pk], apply='1', reason='   ')
        self.assertContains(empty, 'علت رد را بنویسید')
        self.assertEqual(ChequeCreditRequest.objects.filter(status='pending').count(), 2)

    def test_bulk_reject_with_a_reason_rejects_all_and_keeps_permissions_off(self):
        response = self.run_action('reject_requests', [self.request.pk, self.other.pk], apply='1', reason='مدارک ناقص')
        self.assertContains(response, '2 درخواست رد شد')
        for credit_request in (self.request, self.other):
            fresh = self.reload(credit_request)
            self.assertEqual((fresh.status, fresh.rejection_reason), ('rejected', 'مدارک ناقص'))
            self.assertFalse(self.reload(fresh.user).can_purchase_with_check)

    def test_bulk_reject_skips_decided_requests(self):
        service.approve_request(self.other, self.admin)
        self.run_action('reject_requests', [self.request.pk, self.other.pk], apply='1', reason='x')
        self.assertEqual(self.reload(self.other).status, 'approved')
        self.assertEqual(self.reload(self.request).status, 'rejected')

    def test_one_failing_item_does_not_block_the_others(self):
        real = service.approve_request

        def flaky(credit_request, by, **kwargs):
            if credit_request.pk == self.request.pk:
                raise service.ChequeCreditError({'__all__': 'خطای آزمایشی'}, status=409)
            return real(credit_request, by, **kwargs)
        with mock.patch('accounts.cheque_credit_service.approve_request', side_effect=flaky):
            response = self.run_action('approve_requests', [self.request.pk, self.other.pk])
        self.assertContains(response, 'خطای آزمایشی')
        self.assertEqual(self.reload(self.request).status, 'pending')
        self.assertEqual(self.reload(self.other).status, 'approved')


# ------------------------------------------------------------------ پیوند در صفحه‌ی کاربر
class UserAdminLinkTests(AdminBase):
    def user_url(self, user=None):
        return reverse('admin:accounts_customuser_change', args=[(user or self.user).pk])

    def test_the_user_page_links_to_the_latest_request(self):
        page = self.admin_client.get(self.user_url())
        self.assertContains(page, self.change_url())
        self.assertContains(page, 'در انتظار بررسی')
        self.assertContains(page, 'مجموع 1 درخواست')

    def test_the_latest_request_wins(self):
        service.reject_request(self.request, self.admin, 'x')
        newer = self.submit()
        page = self.admin_client.get(self.user_url())
        self.assertContains(page, self.change_url(newer))
        self.assertNotContains(page, f'href="{self.change_url()}"')
        self.assertContains(page, 'مجموع 2 درخواست')

    def test_a_user_without_requests_shows_a_plain_note(self):
        other = make_user('09120000940', price_level=2)
        page = self.admin_client.get(self.user_url(other))
        self.assertContains(page, 'درخواستی ثبت نشده است')

    def test_the_link_is_hidden_without_the_view_permission(self):
        staff = staff_with('view_customuser', 'change_customuser', phone='09120000941')
        page = self.client_for(staff).get(self.user_url())
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, self.change_url())
        self.assertNotContains(page, 'آخرین درخواست خرید چکی')

    def test_the_manual_permission_flag_is_still_editable_on_the_user_page(self):
        page = self.admin_client.get(self.user_url())
        self.assertContains(page, 'name="can_purchase_with_check"')
