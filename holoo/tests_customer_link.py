"""
اتصال کاربر سایت به مشتریِ درستِ هلو: کدملیِ تکراری با موبایلِ دیگر نباید به مشتریِ آدم دیگری وصل کند (ممیزی ۱۸ اکتبر ۲۰۲۶)،
و ابزار ممیزی/اصلاح کاربرانی که قبلاً اشتباه وصل شده‌اند. هیچ تماسی به هلو نیست.
"""
import json
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from accounts.models import CustomUser
from holoo.client import HolooClient, _clear_token_cache
from holoo.customers import find_mislinked_users, unlink_users
from holoo.tests_real_write import LOGIN_OK, real_config, response

HOSSEIN = {'ErpCode': 'HOSSEIN=', 'Code': '03984', 'Name': 'حسين اقدم – 09387344251', 'Mobile': '09387344251', 'BedSarfasl': '1033819'}
EXISTING_NATIONAL = {'Failure': {'ExistingCustomerErpCode': 'HOSSEIN=', 'Error': 'کدملی تکراری', 'ErrorCode': 23}}


class NationalIdDuplicateTests(SimpleTestCase):
    def setUp(self):
        _clear_token_cache()
        self.holoo = HolooClient(real_config())

    def http(self, *replies, existing=HOSSEIN):
        stack = mock.patch.multiple('holoo.client.requests', post=mock.DEFAULT, put=mock.DEFAULT, get=mock.DEFAULT)
        patched = stack.start()
        self.addCleanup(stack.stop)
        patched['post'].side_effect = [LOGIN_OK, *replies]
        patched['get'].return_value = response({'Customer': [existing]})
        return patched

    def test_same_national_id_but_another_mobile_creates_a_separate_customer(self):
        created = response({'Header': 'Customer', 'Success': {'Id': '9', 'ErpCode': 'MINE=', 'Code': '03988', 'BedSarfasl': '1033824'}})
        http = self.http(response(EXISTING_NATIONAL), created)
        result = self.holoo.insert_person('سیدمرتضی', 'موسوی', '09192515466', '0000000000', '', web_id=9)
        self.assertEqual((result['success'], result['erp_code'], result['code'], result['adopted']), (True, 'MINE=', '03988', False))
        retry = http['post'].call_args_list[2]
        self.assertEqual(retry.kwargs['params'], {'allowDuplicateNatCode': 'true'})
        self.assertEqual(json.loads(retry.kwargs['data'].decode('utf-8'))['custinfo'][0]['mobile'], '09192515466')

    def test_same_national_id_and_the_same_mobile_is_still_adopted(self):
        http = self.http(response(EXISTING_NATIONAL))
        result = self.holoo.insert_person('حسین', 'اقدم', '09387344251', '0000000000', '', web_id=9)
        self.assertEqual((result['erp_code'], result['adopted']), ('HOSSEIN=', True))
        self.assertEqual(http['post'].call_count, 2)                        # لاگین + یک ثبت؛ مشتری تازه‌ای ساخته نشد

    def test_existing_customer_without_a_mobile_is_adopted_by_national_id(self):
        self.http(response(EXISTING_NATIONAL), existing={**HOSSEIN, 'Mobile': ''})
        self.assertTrue(self.holoo.insert_person('حسین', 'اقدم', '09120000001', '0012345678', '', web_id=9)['adopted'])

    def test_duplicate_mobile_is_always_the_same_person(self):
        self.http(response({'Failure': {'ExistingCustomerErpCode': 'HOSSEIN=', 'Error': 'موبایل تکراری', 'ErrorCode': 10}}))
        self.assertTrue(self.holoo.insert_person('الف', 'ب', '09387344251', '', '', web_id=9)['adopted'])

    def test_a_failure_of_the_separate_creation_is_reported_as_is(self):
        self.http(response(EXISTING_NATIONAL), response({'Failure': {'Error': 'نام الزامی است', 'ErrorCode': 101}}))
        result = self.holoo.insert_person('', '', '09192515466', '0000000000', '', web_id=9)
        self.assertEqual((result['success'], result['code'], result['transient']), (False, '101', False))


class AuditTests(TestCase):
    def setUp(self):
        self.ok = CustomUser.objects.create_user(phone_number='09387344251', erp_code='HOSSEIN=')
        self.wrong = CustomUser.objects.create_user(phone_number='09192515466', erp_code='HOSSEIN=', holoo_customer_code='03984',
                                                    holoo_bed_sarfasl='1033819')
        self.ghost = CustomUser.objects.create_user(phone_number='09125550001', erp_code='GONE=')
        self.unlinked = CustomUser.objects.create_user(phone_number='09125550002')
        self.rows = [HOSSEIN]

    def test_only_users_pointing_at_someone_elses_customer_or_a_missing_one_are_found(self):
        found = {user.phone_number: reason for user, reason, _ in find_mislinked_users(self.rows)}
        self.assertEqual(found, {'09192515466': 'mobile_differs', '09125550001': 'missing'})

    def test_holoo_mobile_formats_are_normalised_before_comparing(self):
        rows = [{**HOSSEIN, 'Mobile': '+98 938 734 4251'}]
        self.assertNotIn('09387344251', {u.phone_number for u, _, _ in find_mislinked_users(rows)})

    def test_unlinking_clears_the_holoo_fields_and_marks_the_user_for_resync(self):
        ids = unlink_users([item for item in find_mislinked_users(self.rows) if item[1] == 'mobile_differs'])
        self.assertEqual(ids, [self.wrong.pk])
        self.wrong.refresh_from_db()
        self.assertEqual((self.wrong.erp_code, self.wrong.holoo_customer_code, self.wrong.holoo_bed_sarfasl), (None, None, None))
        self.assertEqual(self.wrong.status, 'PENDING_ERP_SYNC')
        self.ok.refresh_from_db()
        self.assertEqual(self.ok.erp_code, 'HOSSEIN=')

    def run_command(self, *args):
        out = StringIO()
        with mock.patch('holoo.management.commands.audit_holoo_customers.fetch_customer_rows', return_value=self.rows), \
             mock.patch('holoo.tasks.sync_user_to_holoo.delay') as delay:
            call_command('audit_holoo_customers', *args, stdout=out)
        return out.getvalue(), delay

    def test_command_reports_without_touching_anything_by_default(self):
        text, delay = self.run_command()
        self.assertIn('09192515466', text)
        self.assertIn('03984', text)
        self.assertIn('--fix', text)
        delay.assert_not_called()
        self.wrong.refresh_from_db()
        self.assertEqual(self.wrong.erp_code, 'HOSSEIN=')

    def test_command_lists_invoices_that_were_registered_for_the_wrong_customer(self):
        from orders.models import Order
        Order.objects.create(user=self.wrong, first_name='م', last_name='م', phone='09192515466', address='x', total_price=1000,
                             holoo_invoice_id='36731')
        text, _ = self.run_command()
        self.assertIn('36731', text)

    def test_fix_unlinks_and_queues_the_resync(self):
        text, delay = self.run_command('--fix')
        self.assertIn('2 کاربر', text)
        self.assertEqual(sorted(call.args[0] for call in delay.call_args_list), sorted([self.wrong.pk, self.ghost.pk]))
        self.wrong.refresh_from_db()
        self.assertIsNone(self.wrong.erp_code)
