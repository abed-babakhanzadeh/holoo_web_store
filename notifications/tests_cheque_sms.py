"""
فاز E: پیامک‌های سفارش چکی — ثبت سفارش چکی، ثبت/اصلاح چک (به مدیر)، تأیید/رد چک و لغو خودکار (به مشتری).
کلیدها در templates_registry (قابل خاموش/روشن و ویرایش متن در پنل)؛ ارسال ناهمگام و پس از commit؛ فقط برای شماره‌ی معتبر؛ با cooldown.
MEDIA_ROOT هر تست موقت است؛ هیچ پیامک واقعی نمی‌رود (تسک ارسال mock است).
"""
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from accounts.testing import make_approved_user
from notifications.models import Notification, NotificationSetting, sync_notification_settings
from notifications.templates_registry import TEMPLATES, render_message
from orders import cheques, deadline
from orders.models import ChequePayment, Order, OrderItem
from orders.tests_cheque_deadline import set_hours
from orders.tests_cheques import VALID, ChequeFlowBase, make_image, upload
from products import stock
from products.models import SiteSettings

OTHER = '6219861099999999'
KEYS = ('cheque_order_placed_customer', 'cheque_registered_admin', 'cheque_approved_customer', 'cheque_rejected_customer',
        'cheque_deadline_canceled_customer')


class SmsBase(ChequeFlowBase):
    def setUp(self):
        super().setUp()
        self.user.first_name = 'علی'
        self.user.save(update_fields=['first_name'])
        self.admin_user = make_approved_user('09120000060', is_staff=True, is_superuser=True)
        patcher = mock.patch('notifications.tasks.deliver_notification.delay')
        self.delay = patcher.start()
        self.addCleanup(patcher.stop)

    def make_order(self, **fields):
        fields.setdefault('cheque_deadline_at', timezone.now() + timedelta(hours=5))
        order = self.cheque_order(**fields)
        OrderItem.objects.create(order=order, product=self.product, price=100000, quantity=2)
        with stock.transaction.atomic():
            stock.reserve_for_order(order.id, {self.product.pk: 2})
        return order

    def notes(self, key=None):
        rows = Notification.objects.order_by('id')
        return list(rows.filter(template_key=key) if key else rows)

    def create(self, order, sayadi=VALID):
        with self.captureOnCommitCallbacks(execute=True):
            return cheques.create_cheque(order, self.user, {'sayadi_id': sayadi}, [upload(make_image())])

    def review(self, cheque, status, reason=''):
        with self.captureOnCommitCallbacks(execute=True):
            return cheques.set_review(cheque, status, self.admin_user, reason)

    def no_cooldown(self):
        cache.clear()


class RegistryTests(SmsBase):
    def test_the_five_keys_are_registered_and_have_panel_rows(self):
        NotificationSetting.objects.all().delete()
        sync_notification_settings()
        for key in KEYS:
            with self.subTest(key=key):
                self.assertIn(key, TEMPLATES)
                self.assertTrue(NotificationSetting.objects.get(template_key=key).is_enabled)

    def test_every_template_renders_with_its_required_parameters(self):
        samples = {'name': 'علی', 'order_id': 12, 'deadline_note': 'ظرف 1 روز', 'cancel_note': ' در غیر این صورت سفارش لغو می‌شود.',
                   'phone': '09120000021', 'action': 'ثبت شد', 'reason': 'ناخوانا'}
        for key in KEYS:
            with self.subTest(key=key):
                text = render_message(key, {name: samples[name] for name in TEMPLATES[key].required})
                self.assertIn('12', text)
                self.assertNotIn('{', text)

    def test_a_missing_parameter_is_a_clear_error(self):
        with self.assertRaises(KeyError):
            render_message('cheque_rejected_customer', {'name': 'x', 'order_id': 1})


class OrderPlacedSmsTests(SmsBase):
    def place(self, option='check'):
        with mock.patch('holoo.receivers.send_order_to_holoo'):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.post_order({'address_id': self.address.pk, 'payment_method': option})
        self.assertEqual(response.status_code, 302)
        return Order.objects.filter(user=self.user).latest('id')

    def test_a_cheque_order_tells_the_customer_to_register_the_cheque(self):
        order = self.place('check')
        notes = self.notes()
        self.assertEqual([n.template_key for n in notes], ['cheque_order_placed_customer'])
        note = notes[0]
        self.assertEqual(note.recipient, self.user.phone_number)
        self.assertIn(str(order.id), note.text)
        self.assertIn('ظرف', note.text)
        self.assertIn('سفارش‌های من', note.text)
        self.assertIn('لغو می‌شود', note.text)
        self.assertEqual(self.delay.call_count, 1)

    def test_a_cash_order_keeps_the_generic_message(self):
        self.place('cash')
        self.assertEqual([n.template_key for n in self.notes()], ['order_placed_customer'])

    def test_without_a_deadline_the_message_makes_no_cancel_threat(self):
        set_hours(0)
        self.place('check')
        text = self.notes('cheque_order_placed_customer')[0].text
        self.assertIn('در اسرع وقت', text)
        self.assertNotIn('لغو', text)

    def test_the_panel_switch_turns_it_off(self):
        sync_notification_settings()
        NotificationSetting.objects.filter(template_key='cheque_order_placed_customer').update(is_enabled=False)
        self.place('check')
        self.assertEqual(self.notes(), [])

    def test_an_admin_custom_text_is_used(self):
        sync_notification_settings()
        NotificationSetting.objects.filter(template_key='cheque_order_placed_customer').update(
            custom_body='سفارش {order_id} - {name} - {deadline_note}{cancel_note}')
        order = self.place('check')
        self.assertTrue(self.notes()[0].text.startswith(f'سفارش {order.id} - علی - ظرف'))


class AdminSmsTests(SmsBase):
    def test_a_new_cheque_notifies_the_admin(self):
        order = self.make_order()
        self.create(order)
        notes = self.notes('cheque_registered_admin')
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].recipient, settings.ADMIN_NOTIFICATION_RECIPIENT)
        self.assertIn(f'#{order.id}', notes[0].text)
        self.assertIn(self.user.phone_number, notes[0].text)
        self.assertIn('ثبت شد', notes[0].text)

    def test_store_admin_numbers_take_priority(self):
        site = SiteSettings.load()
        site.store_admin_sms_recipient, site.store_admin_sms_recipient_2 = '09121111111', '09122222222'
        site.save()
        cache.delete(SiteSettings.CACHE_KEY)
        self.create(self.make_order())
        self.assertEqual(sorted(n.recipient for n in self.notes('cheque_registered_admin')), ['09121111111', '09122222222'])

    def test_rapid_cheques_of_one_order_send_one_admin_sms(self):
        order = self.make_order()
        self.create(order)
        self.create(order, sayadi=OTHER)
        self.assertEqual(len(self.notes('cheque_registered_admin')), 1)
        self.no_cooldown()
        self.create(order, sayadi='6219861011112222')
        self.assertEqual(len(self.notes('cheque_registered_admin')), 2)

    def test_a_corrected_cheque_notifies_the_admin_again(self):
        order = self.make_order()
        cheque = self.create(order)
        self.review(cheque, 'rejected', 'ناخوانا')
        self.no_cooldown()
        with self.captureOnCommitCallbacks(execute=True):
            cheques.update_cheque(ChequePayment.objects.get(pk=cheque.pk), self.user, {'sayadi_id': VALID}, [])
        texts = [n.text for n in self.notes('cheque_registered_admin')]
        self.assertEqual(len(texts), 2)
        self.assertIn('اصلاح و دوباره ارسال شد', texts[-1])

    def test_the_customer_edit_view_triggers_it(self):
        from django.urls import reverse
        order = self.make_order()
        cheque = self.create(order)
        self.review(cheque, 'rejected', 'ناخوانا')
        self.no_cooldown()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse('orders:cheque_edit', args=[order.id, cheque.public_id]), {'sayadi_id': VALID})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(self.notes('cheque_registered_admin')), 2)

    def test_the_panel_switch_turns_it_off(self):
        sync_notification_settings()
        NotificationSetting.objects.filter(template_key='cheque_registered_admin').update(is_enabled=False)
        self.create(self.make_order())
        self.assertEqual(self.notes('cheque_registered_admin'), [])


class ReviewSmsTests(SmsBase):
    def test_approving_the_only_cheque_notifies_the_customer(self):
        order = self.make_order()
        cheque = self.create(order)
        self.review(cheque, 'approved')
        notes = self.notes('cheque_approved_customer')
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].recipient, self.user.phone_number)
        self.assertIn(str(order.id), notes[0].text)
        self.assertIn('تأیید', notes[0].text)

    def test_with_several_cheques_only_the_last_approval_sends(self):
        order = self.make_order()
        first, second = self.create(order), self.create(order, sayadi=OTHER)
        self.review(first, 'approved')
        self.assertEqual(self.notes('cheque_approved_customer'), [])
        self.review(second, 'approved')
        self.assertEqual(len(self.notes('cheque_approved_customer')), 1)

    def test_a_withdrawn_cheque_does_not_hold_back_the_approval_sms(self):
        order = self.make_order()
        first, second = self.create(order), self.create(order, sayadi=OTHER)
        self.review(first, 'rejected', 'ناخوانا')
        cheques.withdraw_cheque(ChequePayment.objects.get(pk=first.pk))
        self.no_cooldown()
        self.review(second, 'approved')
        self.assertEqual(len(self.notes('cheque_approved_customer')), 1)

    def test_rejecting_sends_the_reason_and_the_deadline(self):
        order = self.make_order()
        cheque = self.create(order)
        self.review(cheque, 'rejected', 'تصویر پشت چک ناخوانا است')
        notes = self.notes('cheque_rejected_customer')
        self.assertEqual(len(notes), 1)
        text = notes[0].text
        self.assertIn('تصویر پشت چک ناخوانا است', text)
        self.assertIn(str(order.id), text)
        self.assertIn('ظرف', text)                                              # پنجره‌ی تازه‌ی اصلاح
        self.assertIn('لغو می‌شود', text)

    def test_the_reason_is_sanitized_for_sms(self):
        order = self.make_order()
        cheque = self.create(order)
        self.review(cheque, 'rejected', 'ببینید http://evil.example/x و www.bad.com \n\n' + 'ب' * 200)
        text = self.notes('cheque_rejected_customer')[0].text
        self.assertNotIn('http', text)
        self.assertNotIn('www.', text)
        self.assertNotIn('\n', text)
        self.assertIn('[لینک]', text)
        self.assertIn('…', text)                                                 # به ۸۰ نویسه کوتاه شد
        self.assertLess(len(text), 400)

    def test_back_to_back_rejections_of_one_order_send_one_sms(self):
        order = self.make_order()
        first, second = self.create(order), self.create(order, sayadi=OTHER)
        self.review(first, 'rejected', 'الف')
        self.review(second, 'rejected', 'ب')
        self.assertEqual(len(self.notes('cheque_rejected_customer')), 1)
        self.no_cooldown()
        self.review(ChequePayment.objects.get(pk=first.pk), 'rejected', 'ج')
        self.assertEqual(len(self.notes('cheque_rejected_customer')), 2)

    def test_the_admin_form_and_bulk_action_trigger_it(self):
        from django.test import Client
        from django.urls import reverse
        order = self.make_order()
        cheque = self.create(order)
        admin = Client()
        admin.force_login(self.admin_user)
        with self.captureOnCommitCallbacks(execute=True):
            admin.post(reverse('admin:orders_chequepayment_change', args=[cheque.pk]), {'status': 'rejected', 'rejection_reason': 'مبلغ نمی‌خواند'})
        self.assertIn('مبلغ نمی‌خواند', self.notes('cheque_rejected_customer')[0].text)
        self.no_cooldown()
        other = self.make_order()
        c2 = self.create(other, sayadi=OTHER)
        with self.captureOnCommitCallbacks(execute=True):
            admin.post(reverse('admin:orders_chequepayment_changelist'), {'action': 'approve_cheques', '_selected_action': [c2.pk]})
        self.assertEqual(len(self.notes('cheque_approved_customer')), 1)

    def test_a_refused_review_sends_nothing(self):
        order = self.make_order()
        cheque = self.create(order)
        Order.objects.filter(pk=order.pk).update(approved_at=timezone.now())
        with self.assertRaises(cheques.ChequeError):
            self.review(cheque, 'rejected', 'x')
        self.assertEqual(self.notes('cheque_rejected_customer'), [])

    def test_panel_switches_work_for_both(self):
        sync_notification_settings()
        NotificationSetting.objects.filter(template_key__in=('cheque_approved_customer', 'cheque_rejected_customer')).update(is_enabled=False)
        order = self.make_order()
        first = self.create(order)
        self.review(first, 'rejected', 'x')
        self.no_cooldown()
        self.review(first, 'approved')
        self.assertEqual(self.notes('cheque_rejected_customer') + self.notes('cheque_approved_customer'), [])


class PhoneSafetyTests(SmsBase):
    def test_an_invalid_phone_gets_nothing(self):
        order = self.make_order()
        cheque = self.create(order)
        type(self.user).objects.filter(pk=self.user.pk).update(phone_number='123')
        self.review(cheque, 'rejected', 'x')
        self.assertEqual(self.notes('cheque_rejected_customer'), [])

    def test_a_deadline_cancel_with_an_invalid_phone_sends_nothing(self):
        order = self.make_order(cheque_deadline_at=timezone.now() - timedelta(hours=1))
        type(self.user).objects.filter(pk=self.user.pk).update(phone_number='abc')
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(deadline.cancel_expired_cheque_orders(), 1)
        self.assertEqual(self.notes('cheque_deadline_canceled_customer'), [])
        self.assertEqual(Order.objects.get(pk=order.pk).status, 'canceled')            # لغو بدون پیامک هم انجام می‌شود

    def test_the_recipient_is_the_normalized_number(self):
        order = self.make_order()
        cheque = self.create(order)
        type(self.user).objects.filter(pk=self.user.pk).update(phone_number='9120000021')
        self.review(cheque, 'approved')
        self.assertEqual(self.notes('cheque_approved_customer')[0].recipient, '09120000021')


class AutoCancelSmsTests(SmsBase):
    def sweep(self):
        with self.captureOnCommitCallbacks(execute=True):
            return deadline.cancel_expired_cheque_orders()

    def test_the_auto_cancel_notifies_the_customer_once(self):
        order = self.make_order(cheque_deadline_at=timezone.now() - timedelta(minutes=5))
        self.assertEqual(self.sweep(), 1)
        self.assertEqual(self.sweep(), 0)
        notes = self.notes('cheque_deadline_canceled_customer')
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].recipient, self.user.phone_number)
        self.assertIn(str(order.id), notes[0].text)
        self.assertIn('لغو شد', notes[0].text)

    def test_nothing_is_sent_for_orders_that_stay_open(self):
        self.make_order()
        self.assertEqual(self.sweep(), 0)
        self.assertEqual(self.notes(), [])

    def test_the_celery_task_path_sends_it(self):
        from orders.tasks import cancel_expired_cheque_orders
        self.make_order(cheque_deadline_at=timezone.now() - timedelta(minutes=5))
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(cancel_expired_cheque_orders(), 'canceled=1')
        self.assertEqual(len(self.notes('cheque_deadline_canceled_customer')), 1)

    def test_a_failing_sms_never_breaks_the_cancel(self):
        order = self.make_order(cheque_deadline_at=timezone.now() - timedelta(minutes=5))
        with mock.patch('notifications.receivers.notify', side_effect=RuntimeError('boom')):
            self.assertEqual(self.sweep(), 1)
        self.assertEqual(Order.objects.get(pk=order.pk).status, 'canceled')

    def test_the_panel_switch_turns_it_off(self):
        sync_notification_settings()
        NotificationSetting.objects.filter(template_key='cheque_deadline_canceled_customer').update(is_enabled=False)
        self.make_order(cheque_deadline_at=timezone.now() - timedelta(minutes=5))
        self.sweep()
        self.assertEqual(self.notes(), [])

    def test_a_correction_window_expiry_also_sends(self):
        order = self.make_order()
        cheque = self.create(order)
        self.review(cheque, 'rejected', 'x')
        Order.objects.filter(pk=order.pk).update(cheque_deadline_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.sweep(), 1)
        self.assertEqual(len(self.notes('cheque_deadline_canceled_customer')), 1)
