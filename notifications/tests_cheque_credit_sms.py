"""
فاز F4: پیامک‌های درخواست خرید چکی / اعتباری — ثبت (به مدیر)، تأیید و رد با علت (به مشتری). کلیدها در templates_registry (قابل خاموش/روشن
و ویرایش متن در پنل)؛ ارسال ناهمگام و پس از commit؛ فقط برای شماره‌ی معتبر؛ با cooldown. تسک ارسال mock است؛ پیامک واقعی نمی‌رود.
MEDIA_ROOT هر تست موقت است.
"""
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from accounts import cheque_credit_service as service
from accounts.cheque_credit import ChequeCreditRequest
from accounts.models import CustomUser
from accounts.tests_cheque_credit import CreditBase, docs, form_data, make_user
from notifications.models import Notification, NotificationSetting, sync_notification_settings
from notifications.templates_registry import TEMPLATES, render_message
from orders.tests_cheques import make_image, upload
from products.models import SiteSettings

KEYS = ('cheque_credit_requested_admin', 'cheque_credit_approved_customer', 'cheque_credit_rejected_customer')


class CreditSmsBase(CreditBase):
    def setUp(self):
        super().setUp()
        CustomUser.objects.filter(pk=self.user.pk).update(first_name='علی')
        self.user = self.reload(self.user)
        patcher = mock.patch('notifications.tasks.deliver_notification.delay')
        self.delay = patcher.start()
        self.addCleanup(patcher.stop)

    def notes(self, key=None):
        rows = Notification.objects.order_by('id')
        return list(rows.filter(template_key=key) if key else rows)

    def approve(self, request):
        with self.captureOnCommitCallbacks(execute=True):
            return service.approve_request(request, self.admin)

    def reject(self, request, reason='مدارک ناقص است'):
        with self.captureOnCommitCallbacks(execute=True):
            return service.reject_request(request, self.admin, reason)

    def no_cooldown(self):
        cache.clear()


class RegistryTests(CreditSmsBase):
    def test_the_three_keys_are_registered_enabled_and_have_panel_rows(self):
        NotificationSetting.objects.all().delete()
        sync_notification_settings()
        for key in KEYS:
            with self.subTest(key=key):
                self.assertIn(key, TEMPLATES)
                self.assertTrue(NotificationSetting.objects.get(template_key=key).is_enabled)

    def test_every_template_renders_and_a_missing_parameter_is_a_clear_error(self):
        samples = {'name': 'علی', 'phone': '09120000501', 'reason': 'ناخوانا'}
        for key in KEYS:
            with self.subTest(key=key):
                text = render_message(key, {name: samples[name] for name in TEMPLATES[key].required})
                self.assertNotIn('{', text)
        with self.assertRaises(KeyError):
            render_message('cheque_credit_rejected_customer', {'name': 'x'})

    def test_the_texts_say_the_right_things(self):
        self.assertIn('تأیید شد', TEMPLATES['cheque_credit_approved_customer'].body)
        self.assertIn('چکی', TEMPLATES['cheque_credit_approved_customer'].body)
        rejected = TEMPLATES['cheque_credit_rejected_customer'].body
        self.assertIn('{reason}', rejected)
        self.assertIn('دوباره درخواست', rejected)


class RequestedSmsTests(CreditSmsBase):
    def test_a_new_request_notifies_the_admin(self):
        self.submit()
        notes = self.notes('cheque_credit_requested_admin')
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].recipient, settings.ADMIN_NOTIFICATION_RECIPIENT)
        self.assertIn(self.user.phone_number, notes[0].text)
        self.assertIn('علی رضایی', notes[0].text)
        self.assertEqual(self.delay.call_count, 1)

    def test_store_admin_numbers_take_priority(self):
        site = SiteSettings.load()
        site.store_admin_sms_recipient, site.store_admin_sms_recipient_2 = '09121111111', '09122222222'
        site.save()
        cache.delete(SiteSettings.CACHE_KEY)
        self.submit()
        self.assertEqual(sorted(n.recipient for n in self.notes('cheque_credit_requested_admin')), ['09121111111', '09122222222'])

    def test_nothing_is_sent_when_the_submission_is_refused(self):
        cheque_customer = make_user('09120000960', price_level=1)
        with self.assertRaises(service.ChequeCreditError):
            service.submit_request(cheque_customer, form_data(), docs())
        with self.assertRaises(service.ChequeCreditError):
            service.submit_request(self.user, form_data(iban='1'), docs())
        self.assertEqual(self.notes(), [])

    def test_the_customer_view_triggers_it(self):
        client = Client()
        client.force_login(self.user)
        data = form_data()
        data['doc_cheque_book'] = [upload(make_image(), 'a.jpg')]
        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(reverse('accounts:cheque_credit'), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(self.notes('cheque_credit_requested_admin')), 1)

    def test_a_request_resubmitted_right_after_a_cancel_does_not_spam_the_admin(self):
        first = self.submit()
        with self.captureOnCommitCallbacks(execute=True):
            service.cancel_request(first, self.user)
        self.submit()
        self.assertEqual(len(self.notes('cheque_credit_requested_admin')), 1)          # cooldown ۱۰ دقیقه‌ای برای هر کاربر
        self.no_cooldown()
        with self.captureOnCommitCallbacks(execute=True):
            service.cancel_request(ChequeCreditRequest.objects.get(status='pending'), self.user)
        self.submit()
        self.assertEqual(len(self.notes('cheque_credit_requested_admin')), 2)

    def test_the_panel_switch_turns_it_off(self):
        sync_notification_settings()
        NotificationSetting.objects.filter(template_key='cheque_credit_requested_admin').update(is_enabled=False)
        self.submit()
        self.assertEqual(self.notes(), [])

    def test_the_name_is_sanitized(self):
        CustomUser.objects.filter(pk=self.user.pk).update(first_name='http://evil.example/x', last_name='ب' * 45)
        self.submit(user=self.reload(self.user))
        text = self.notes('cheque_credit_requested_admin')[0].text
        self.assertNotIn('http', text)
        self.assertIn('[لینک]', text)


class ApprovedSmsTests(CreditSmsBase):
    def test_approval_notifies_the_customer(self):
        self.approve(self.submit())
        notes = self.notes('cheque_credit_approved_customer')
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].recipient, self.user.phone_number)
        self.assertIn('علی', notes[0].text)
        self.assertIn('تأیید شد', notes[0].text)
        self.assertIn('چکی', notes[0].text)

    def test_the_admin_form_and_bulk_action_trigger_it(self):
        admin_client = Client()
        admin_client.force_login(self.admin)
        request = self.submit()
        with self.captureOnCommitCallbacks(execute=True):
            admin_client.post(reverse('admin:accounts_chequecreditrequest_change', args=[request.pk]),
                              {'status': 'approved', 'rejection_reason': '', 'approved_limit': '50000000', 'admin_note': '', '_save': '1'})
        self.assertEqual(len(self.notes('cheque_credit_approved_customer')), 1)
        other = self.submit(user=make_user('09120000961', price_level=2, first_name='مریم'))
        with self.captureOnCommitCallbacks(execute=True):
            admin_client.post(reverse('admin:accounts_chequecreditrequest_changelist'),
                              {'action': 'approve_requests', '_selected_action': [other.pk]})
        self.assertEqual(len(self.notes('cheque_credit_approved_customer')), 2)

    def test_approving_twice_sends_one_sms(self):
        request = self.submit()
        self.approve(request)
        with self.assertRaises(service.ChequeCreditError):
            self.approve(request)
        self.assertEqual(len(self.notes('cheque_credit_approved_customer')), 1)

    def test_an_invalid_phone_gets_nothing_but_the_approval_stands(self):
        request = self.submit()
        CustomUser.objects.filter(pk=self.user.pk).update(phone_number='123')
        self.approve(request)
        self.assertEqual(self.notes('cheque_credit_approved_customer'), [])
        self.assertTrue(self.reload(self.user).can_purchase_with_check)

    def test_the_recipient_is_the_normalized_number(self):
        request = self.submit()
        CustomUser.objects.filter(pk=self.user.pk).update(phone_number='9120000501')
        self.approve(request)
        self.assertEqual(self.notes('cheque_credit_approved_customer')[0].recipient, '09120000501')

    def test_a_failing_sms_never_breaks_the_approval(self):
        request = self.submit()
        with mock.patch('notifications.receivers.notify', side_effect=RuntimeError('boom')):
            self.approve(request)
        self.assertEqual(self.reload(request).status, 'approved')
        self.assertTrue(self.reload(self.user).can_purchase_with_check)

    def test_the_panel_switch_and_custom_text_work(self):
        sync_notification_settings()
        NotificationSetting.objects.filter(template_key='cheque_credit_approved_customer').update(custom_body='سلام {name}، مجوز فعال شد')
        self.approve(self.submit())
        self.assertEqual(self.notes('cheque_credit_approved_customer')[0].text, 'سلام علی، مجوز فعال شد')
        NotificationSetting.objects.filter(template_key='cheque_credit_approved_customer').update(is_enabled=False)
        other = self.submit(user=make_user('09120000962', price_level=2))
        self.approve(other)
        self.assertEqual(len(self.notes('cheque_credit_approved_customer')), 1)


class RejectedSmsTests(CreditSmsBase):
    def test_rejection_sends_the_reason(self):
        self.reject(self.submit(), 'تصویر پشت چک ناخوانا است')
        notes = self.notes('cheque_credit_rejected_customer')
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].recipient, self.user.phone_number)
        self.assertIn('تصویر پشت چک ناخوانا است', notes[0].text)
        self.assertIn('دوباره درخواست', notes[0].text)
        self.assertFalse(self.reload(self.user).can_purchase_with_check)

    def test_the_reason_is_sanitized_for_sms(self):
        self.reject(self.submit(), 'ببینید http://evil.example/x و www.bad.com \n\n' + 'ب' * 200)
        text = self.notes('cheque_credit_rejected_customer')[0].text
        self.assertNotIn('http', text)
        self.assertNotIn('www.', text)
        self.assertNotIn('\n', text)
        self.assertIn('[لینک]', text)
        self.assertIn('…', text)
        self.assertLess(len(text), 400)

    def test_a_refused_rejection_sends_nothing(self):
        request = self.submit()
        with self.assertRaises(service.ChequeCreditError):
            self.reject(request, '   ')
        self.assertEqual(self.notes('cheque_credit_rejected_customer'), [])

    def test_rejecting_a_decided_request_sends_nothing_more(self):
        request = self.submit()
        self.reject(request)
        with self.assertRaises(service.ChequeCreditError):
            self.reject(request, 'دوباره')
        self.assertEqual(len(self.notes('cheque_credit_rejected_customer')), 1)

    def test_back_to_back_decisions_for_one_user_send_one_sms_each_kind(self):
        first = self.submit()
        self.reject(first)
        second = self.submit()
        self.reject(second, 'علت دوم')
        self.assertEqual(len(self.notes('cheque_credit_rejected_customer')), 1)          # cooldown ۶۰ ثانیه‌ای
        self.no_cooldown()
        third = self.submit()
        self.reject(third, 'علت سوم')
        self.assertEqual(len(self.notes('cheque_credit_rejected_customer')), 2)

    def test_the_bulk_reject_action_triggers_it(self):
        admin_client = Client()
        admin_client.force_login(self.admin)
        request = self.submit()
        with self.captureOnCommitCallbacks(execute=True):
            admin_client.post(reverse('admin:accounts_chequecreditrequest_changelist'),
                              {'action': 'reject_requests', '_selected_action': [request.pk], 'apply': '1', 'reason': 'مبلغ نمی‌خواند'})
        self.assertIn('مبلغ نمی‌خواند', self.notes('cheque_credit_rejected_customer')[0].text)

    def test_the_panel_switch_turns_it_off(self):
        sync_notification_settings()
        NotificationSetting.objects.filter(template_key='cheque_credit_rejected_customer').update(is_enabled=False)
        self.reject(self.submit())
        self.assertEqual(self.notes('cheque_credit_rejected_customer'), [])
