"""
سرفصل کارتخوان در تنظیمات سایت (اتفاق ۱۳ اکتبر ۲۰۲۶): مدیر «شماره حساب» را به‌جای «کد سرفصل» وارد کرد و هلو همه‌ی فاکتورهای
پرداخت‌شده را با خطای ۳۲ رد کرد. این فایل ترجمه‌ی شماره‌حساب ← سرفصل، بازیابی خودکار پس از خطای ۳۲، فهرست انتخاب ادمین،
صف‌کردن دوباره‌ی سفارش‌های ردشده با اصلاح تنظیم، و نمایش مرحله‌ی حسابداری سفارش را می‌سنجد. به شبکه/هلو واقعی نمی‌رود.
"""
from unittest import mock

from django.contrib import admin as django_admin
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomUser
from holoo import accounts as holoo_accounts
from holoo.tasks import confirm_payment_in_holoo, send_order_to_holoo
from holoo.tests_real_write import TaskBase
from orders.models import Order
from products.admin import SiteSettingsAdmin
from products.models import SiteSettings

ACCOUNTS = [
    {'sarfaslCode': '10200010004', 'bankName': 'ملي', 'accountNumber': '0110062796003', 'hasPOS': True,
     'cardNumber': '6037991784799995', 'branchName': 'عمارياسر'},
    {'sarfaslCode': '10200010019', 'bankName': 'ملي', 'accountNumber': '1111111111', 'hasPOS': True, 'branchName': 'سايت'},
    {'sarfaslCode': '10200010001', 'bankName': 'ملي', 'accountNumber': '0370226057008', 'hasPOS': False, 'branchName': 'عمارياسر'},
    {'sarfaslCode': '10200010005', 'bankName': 'صادرات', 'accountNumber': '0103920529000', 'hasPOS': False},
    {'sarfaslCode': '10200010099', 'bankName': 'سپه', 'accountNumber': '0103920529000', 'hasPOS': True},   # شماره‌ی تکراری
]


def parsed():
    client = mock.Mock()
    client.get_json.return_value = ACCOUNTS
    return holoo_accounts.list_bank_accounts(client)


class AccountParsingTests(SimpleTestCase):
    def test_rows_are_normalised(self):
        accounts = parsed()
        first = accounts[0]
        self.assertEqual((first['sarfasl'], first['bank'], first['branch'], first['has_pos']),
                         ('10200010004', 'ملی', 'عماریاسر', True))        # ي ← ی (کدپیج هلو)
        self.assertEqual(len(accounts), 5)

    def test_unreachable_or_mock_holoo_gives_none(self):
        client = mock.Mock()
        client.get_json.return_value = None
        self.assertIsNone(holoo_accounts.list_bank_accounts(client))
        client.get_json.return_value = {'unexpected': True}
        self.assertIsNone(holoo_accounts.list_bank_accounts(client))

    def test_match_by_sarfasl_by_account_number_and_ambiguity(self):
        accounts = parsed()
        account, how = holoo_accounts.match_account('10200010004', accounts)
        self.assertEqual((account['sarfasl'], how), ('10200010004', holoo_accounts.BY_SARFASL))
        account, how = holoo_accounts.match_account('۰۱۱۰۰۶۲۷۹۶۰۰۳', accounts)            # رقم فارسی هم پذیرفته می‌شود
        self.assertEqual((account['sarfasl'], how), ('10200010004', holoo_accounts.BY_ACCOUNT_NUMBER))
        self.assertEqual(holoo_accounts.match_account(' 1111-111111 ', accounts)[0]['sarfasl'], '10200010019')
        self.assertEqual(holoo_accounts.match_account('0103920529000', accounts), (None, None))      # مبهم ← حدس نمی‌زنیم
        self.assertEqual(holoo_accounts.match_account('', accounts), (None, None))
        self.assertEqual(holoo_accounts.match_account('999', accounts), (None, None))

    def test_resolve_only_returns_pos_enabled_sarfasl(self):
        client = mock.Mock()
        client.get_json.return_value = ACCOUNTS
        self.assertEqual(holoo_accounts.resolve_pos_sarfasl('0110062796003', client), '10200010004')
        self.assertEqual(holoo_accounts.resolve_pos_sarfasl('0370226057008', client), None)    # حساب بدون کارت‌خوان
        client.get_json.return_value = None
        self.assertIsNone(holoo_accounts.resolve_pos_sarfasl('0110062796003', client))

    def test_label_contains_bank_branch_account_and_sarfasl(self):
        label = holoo_accounts.account_label(parsed()[0])
        for part in ('ملی', 'عماریاسر', '0110062796003', '10200010004'):
            self.assertIn(part, label)


class InvoiceHealingTests(TaskBase):
    OK = {'success': True, 'InvoiceCode': '36707', 'ErpCode': 'bBAD=', 'SanadCode': '74090', 'adopted': False}
    BAD_POS = {'success': False, 'code': '32', 'transient': False,
               'message': 'سرفصل كارت خوان معتبر نمي باشد: Holoo2 - 0110062796003'}

    def setUp(self):
        super().setUp()
        CustomUser.objects.filter(pk=self.user.pk).update(erp_code='CUST=')

    def configure(self, value):
        SiteSettings.objects.update_or_create(pk=1, defaults={'holoo_pos_sarfasl': value})
        cache.delete(SiteSettings.CACHE_KEY)

    def run_task(self, order, results, accounts=ACCOUNTS):
        with mock.patch('holoo.client.HolooClient.insert_invoice', side_effect=results) as insert, \
             mock.patch('holoo.client.HolooClient.get_json', return_value=accounts), \
             mock.patch('notifications.service.notify_admin') as alert, \
             mock.patch('holoo.tasks.send_order_to_holoo.retry', side_effect=RuntimeError('retried')):
            outcome = send_order_to_holoo.run(order.id)
        return outcome, insert, alert

    def test_account_number_is_translated_and_the_invoice_is_resent_once(self):
        self.configure('0110062796003')
        order = self.make_order(paid=True, method='cash')
        outcome, insert, alert = self.run_task(order, [self.BAD_POS, self.OK])
        self.assertEqual(outcome, 'Success: 36707')
        self.assertEqual([call.args[0]['PosSarfasl'] for call in insert.call_args_list], ['0110062796003', '10200010004'])
        order.refresh_from_db()
        self.assertEqual((order.holoo_invoice_id, order.status, order.holoo_needs_attention), ('36707', 'registered', False))
        alert.assert_not_called()
        self.assertEqual(SiteSettings.objects.get(pk=1).holoo_pos_sarfasl, '10200010004')        # تنظیم هم اصلاح شد
        self.assertEqual(SiteSettings.cached().holoo_pos_sarfasl, '10200010004')

    def test_first_call_carries_the_configured_value_unchanged(self):
        self.configure('0110062796003')
        order = self.make_order(paid=True, method='cash')
        _, insert, _ = self.run_task(order, [self.BAD_POS, self.OK])
        self.assertEqual(insert.call_args_list[0].args[0]['PosSarfasl'], '0110062796003')
        self.assertEqual(insert.call_count, 2)

    def test_unresolvable_value_is_flagged_with_a_helpful_message_and_not_retried(self):
        self.configure('555')
        order = self.make_order(paid=True, method='cash')
        outcome, insert, alert = self.run_task(order, [self.BAD_POS])
        self.assertTrue(outcome.startswith('Needs attention'))
        self.assertEqual(insert.call_count, 1)
        order.refresh_from_db()
        self.assertTrue(order.holoo_needs_attention)
        self.assertIn('کد 32', order.holoo_last_error)
        self.assertIn('کد سرفصل', order.holoo_last_error)                                    # راهنمای اصلاح
        self.assertEqual(order.accounting_state[0], Order.ACCOUNTING_ATTENTION)
        alert.assert_called_once()
        self.assertEqual(SiteSettings.objects.get(pk=1).holoo_pos_sarfasl, '555')             # دست‌نخورده

    def test_non_pos_account_is_not_silently_used(self):
        self.configure('0370226057008')                                                       # حساب بدون کارت‌خوان
        order = self.make_order(paid=True, method='cash')
        outcome, insert, _ = self.run_task(order, [self.BAD_POS])
        self.assertTrue(outcome.startswith('Needs attention'))
        self.assertEqual(insert.call_count, 1)

    def test_holoo_down_during_recovery_still_flags_the_order(self):
        self.configure('0110062796003')
        order = self.make_order(paid=True, method='cash')
        outcome, insert, _ = self.run_task(order, [self.BAD_POS], accounts=None)
        self.assertTrue(outcome.startswith('Needs attention'))
        self.assertEqual(insert.call_count, 1)

    def test_credit_invoice_never_goes_through_pos_recovery(self):
        order = self.make_order(paid=False, method='check')
        with mock.patch('holoo.accounts.resolve_pos_sarfasl') as resolve:
            self.run_task(order, [self.BAD_POS])
        resolve.assert_not_called()

    def test_adopted_invoice_without_sanad_still_gets_a_receipt_marker(self):
        """ فاکتورِ پذیرفته‌شده (۱۰۲) ممکن است شماره‌ی سند نداشته باشد؛ بدون نشانگر، مسیر «سند جدا» برای همیشه retry می‌شد """
        self.configure('10200010004')
        order = self.make_order(paid=True, method='cash')
        adopted = {'success': True, 'InvoiceCode': '36707', 'ErpCode': 'bBAD=', 'SanadCode': '', 'adopted': True}
        with mock.patch('holoo.tasks.confirm_payment_in_holoo.delay') as confirm:
            self.run_task(order, [adopted])
        order.refresh_from_db()
        self.assertEqual(order.holoo_receipt_id, '36707')
        confirm.assert_not_called()


class SeparateReceiptTaskTests(TaskBase):
    def make_invoiced(self):
        order = self.make_order(paid=True, method='cash', holoo_invoice_id='36707')
        return order

    def test_not_implemented_in_real_mode_ends_without_retry_loop(self):
        order = self.make_invoiced()
        result = {'success': False, 'code': 'NOT_IMPLEMENTED', 'transient': False, 'message': 'x'}
        with mock.patch('holoo.client.HolooClient.register_payment', return_value=result), \
             mock.patch('holoo.tasks.confirm_payment_in_holoo.retry', side_effect=RuntimeError('retried')):
            outcome = confirm_payment_in_holoo.run(order.id)
        self.assertIn('Settled', outcome)
        order.refresh_from_db()
        self.assertEqual(order.holoo_receipt_id, '36707')
        self.assertEqual(order.status, 'pending')                # وضعیت را این مسیر جابه‌جا نمی‌کند


class AccountingStateTests(TaskBase):
    def test_states_follow_approval_invoice_and_error(self):
        order = self.make_order(paid=True, method='cash')
        self.assertEqual(order.accounting_state[0], Order.ACCOUNTING_QUEUED)
        Order.objects.filter(pk=order.pk).update(holoo_needs_attention=True, holoo_last_error='خطا')
        order.refresh_from_db()
        self.assertEqual(order.accounting_state[0], Order.ACCOUNTING_ATTENTION)
        Order.objects.filter(pk=order.pk).update(holoo_invoice_id='36707')
        order.refresh_from_db()
        self.assertEqual(order.accounting_state, (Order.ACCOUNTING_INVOICED, 'فاکتور 36707 ثبت شد'))
        Order.objects.filter(pk=order.pk).update(approved_at=None, holoo_invoice_id=None)
        order.refresh_from_db()
        self.assertEqual(order.accounting_state[0], Order.ACCOUNTING_AWAITING_APPROVAL)
        Order.objects.filter(pk=order.pk).update(status='canceled')
        order.refresh_from_db()
        self.assertEqual(order.accounting_state[0], Order.ACCOUNTING_CLOSED)


class SiteSettingsFieldTests(TaskBase):
    def field(self, accounts):
        db_field = SiteSettings._meta.get_field('holoo_pos_sarfasl')
        model_admin = SiteSettingsAdmin(SiteSettings, django_admin.site)
        with mock.patch('holoo.client.HolooClient.get_json', return_value=accounts):
            return model_admin._holoo_pos_sarfasl_field(db_field)

    def set_current(self, value):
        SiteSettings.objects.update_or_create(pk=1, defaults={'holoo_pos_sarfasl': value})
        cache.delete(SiteSettings.CACHE_KEY)

    def test_only_pos_enabled_accounts_are_offered(self):
        self.set_current('10200010004')
        field = self.field(ACCOUNTS)
        values = [value for value, _label in field.choices]
        self.assertEqual(values, ['10200010004', '10200010019', '10200010099'])
        self.assertTrue(field.required)

    def test_account_number_as_current_value_gets_a_placeholder_naming_the_right_account(self):
        self.set_current('0110062796003')
        field = self.field(ACCOUNTS)
        first_value, first_label = field.choices[0]
        self.assertEqual(first_value, '')
        self.assertIn('شماره‌حساب', first_label)
        self.assertIn('10200010004', first_label)
        with self.assertRaises(Exception):
            field.clean('')                                      # باید حتماً انتخاب شود
        self.assertEqual(field.clean('10200010019'), '10200010019')
        with self.assertRaises(Exception):
            field.clean('0110062796003')                         # شماره حساب پذیرفته نمی‌شود

    def test_without_a_reachable_holoo_the_plain_text_field_remains(self):
        self.set_current('10200010004')
        self.assertIsNone(self.field(None))

    def request(self):
        request = RequestFactory().post('/admin/')
        request.session = {}
        request._messages = FallbackStorage(request)
        return request

    def test_changing_the_setting_requeues_orders_rejected_for_it(self):
        CustomUser.objects.filter(pk=self.user.pk).update(erp_code='CUST=')
        rejected = self.make_order(paid=True, method='cash')
        Order.objects.filter(pk=rejected.pk).update(holoo_needs_attention=True,
                                                    holoo_last_error='هلو فاکتور را رد کرد (کد 32): سرفصل کارت خوان معتبر نمي باشد')
        other = self.make_order(paid=True, method='cash')
        Order.objects.filter(pk=other.pk).update(holoo_needs_attention=True, holoo_last_error='هلو فاکتور را رد کرد (کد 15): کالا')
        invoiced = self.make_order(paid=True, method='cash', holoo_invoice_id='1')
        Order.objects.filter(pk=invoiced.pk).update(holoo_needs_attention=True, holoo_last_error='(کد 32)')
        with mock.patch('holoo.tasks.send_order_to_holoo.delay') as delay, self.captureOnCommitCallbacks(execute=True):
            SiteSettingsAdmin._requeue_orders_rejected_for_pos(self.request())
        delay.assert_called_once_with(rejected.pk)
        rejected.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual((rejected.holoo_needs_attention, rejected.holoo_last_error), (False, ''))
        self.assertTrue(other.holoo_needs_attention)                                   # خطای دیگر دست نمی‌خورد
        self.assertTrue(Order.objects.get(pk=invoiced.pk).holoo_needs_attention)


class OrderAdminVisibilityTests(TaskBase):
    def setUp(self):
        super().setUp()
        self.admin_user = CustomUser.objects.create_superuser(phone_number='09120000802', password='x')
        self.client.force_login(self.admin_user)

    def test_list_shows_the_accounting_stage_with_the_error_state(self):
        order = self.make_order(paid=True, method='cash')
        Order.objects.filter(pk=order.pk).update(holoo_needs_attention=True, holoo_last_error='خطا')
        response = self.client.get(reverse('admin:orders_order_changelist'))
        self.assertContains(response, 'هلو فاکتور را رد کرد؛ نیاز به بررسی')

    def test_change_page_shows_the_error_and_a_retry_button(self):
        order = self.make_order(paid=True, method='cash')
        Order.objects.filter(pk=order.pk).update(holoo_needs_attention=True, holoo_last_error='هلو فاکتور را رد کرد (کد 32): سرفصل')
        response = self.client.get(reverse('admin:orders_order_change', args=[order.pk]))
        self.assertContains(response, 'data-testid="holoo-error"')
        self.assertContains(response, 'سرفصل')
        self.assertContains(response, reverse('admin:orders_order_retry_holoo', args=[order.pk]))

    def test_change_page_of_an_invoiced_order_has_no_retry_button(self):
        order = self.make_order(paid=True, method='cash', holoo_invoice_id='36707')
        response = self.client.get(reverse('admin:orders_order_change', args=[order.pk]))
        self.assertNotContains(response, reverse('admin:orders_order_retry_holoo', args=[order.pk]))
        self.assertNotContains(response, 'data-testid="holoo-error"')

    def test_retry_button_requeues_the_order_and_is_post_only(self):
        order = self.make_order(paid=True, method='cash')
        Order.objects.filter(pk=order.pk).update(holoo_needs_attention=True, holoo_last_error='خطا')
        url = reverse('admin:orders_order_retry_holoo', args=[order.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        with mock.patch('holoo.tasks.send_order_to_holoo.delay') as delay:
            response = self.client.post(url, follow=True)
        delay.assert_called_once_with(order.pk)
        self.assertContains(response, 'دوباره برای ثبت فاکتور در هلو به صف رفت')
        order.refresh_from_db()
        self.assertEqual((order.holoo_needs_attention, order.holoo_last_error), (False, ''))
