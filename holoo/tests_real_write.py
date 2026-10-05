"""
ثبت واقعی مشتری و فاکتور در هلو (فاز پیاده‌سازی): فهرست سفید دیتابیس، ترجمه به قرارداد واقعی (wire)، کلاینت در حالت real
(با HTTP ساختگی)، طبقه‌بندی خطا (دائمی/موقت)، پذیرش مشتری/فاکتور موجود، و تسک‌های وابسته به تأیید مدیر.
هیچ‌کدام از این تست‌ها به شبکه نمی‌روند؛ اجرای واقعی روی Holoo2 جداست.
"""
import json
from datetime import datetime, timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from accounts.models import Address, CustomUser
from holoo import wire
from holoo.client import HolooClient, _clear_token_cache
from holoo.conf import HolooConfig, get_config
from holoo.tasks import reconcile_holoo_orders, send_order_to_holoo, sync_user_to_holoo
from locations.models import City, Province
from orders.models import Order, OrderItem
from payments.models import Transaction
from products import stock
from products.models import Category, Product, SiteSettings, StockReservation


def real_config(db='Holoo2', **extra):
    data = dict(base_url='http://holoo.test/api', username='web', password='MQ==', db_name=db, read_mode='real',
                write_mode='real', write_allowed_dbs=('Holoo2',), timeout=5)
    data.update(extra)
    return HolooConfig(**data)


def response(payload=None, status=200, text=''):
    r = mock.Mock(status_code=status, text=text or json.dumps(payload, ensure_ascii=False))
    r.json.return_value = payload
    if payload is None:
        r.json.side_effect = ValueError('no json')
    return r


LOGIN_OK = response({'Login': {'State': 'True', 'Token': 'Bearer x.e30.y', 'ErrorCode': 0, 'Error': ''}})


class WriteAllowListTests(SimpleTestCase):
    def cfg(self, env):
        class S:
            HOLOO_API_URL = 'http://x/api'; HOLOO_USERNAME = 'u'; HOLOO_PASSWORD = 'p'; HOLOO_DB_NAME = 'Holoo2'
        return get_config(env=env, env_file='/nonexistent/.env', settings_obj=S)

    def test_real_is_accepted_only_for_the_allow_listed_test_database(self):
        config = self.cfg({'HOLOO_WRITE_MODE': 'real'})
        self.assertEqual((config.write_mode, config.write_is_real), ('real', True))
        self.assertEqual(config.write_allowed_dbs, ('Holoo2',))

    def test_real_on_any_other_database_fails_closed_with_a_warning(self):
        config = self.cfg({'HOLOO_WRITE_MODE': 'real', 'HOLOO_DB_NAME': 'Holoo1'})
        self.assertEqual(config.write_mode, 'disabled')
        self.assertTrue(any('Holoo1' in w for w in config.warnings))

    def test_a_production_database_needs_an_explicit_allow_list_entry(self):
        config = self.cfg({'HOLOO_WRITE_MODE': 'real', 'HOLOO_DB_NAME': 'HolooMain', 'HOLOO_WRITE_ALLOWED_DBS': 'Holoo2, HolooMain'})
        self.assertEqual(config.write_mode, 'real')

    def test_db_name_comparison_ignores_case(self):
        self.assertEqual(self.cfg({'HOLOO_WRITE_MODE': 'real', 'HOLOO_DB_NAME': 'holoo2'}).write_mode, 'real')

    def test_real_writes_require_real_reads(self):
        config = self.cfg({'HOLOO_WRITE_MODE': 'real', 'HOLOO_READ_MODE': 'mock'})
        self.assertEqual(config.write_mode, 'disabled')

    def test_default_stays_mock_so_nothing_is_written_unless_asked(self):
        self.assertEqual(self.cfg({}).write_mode, 'mock')


class WireTests(SimpleTestCase):
    def payload(self, **extra):
        data = {
            'OrderId': 84, 'CustomerErpCode': 'CUST=', 'IssuedAt': datetime(2026, 10, 4, 9, 5, 59), 'Comment': 'سفارش آنلاین سایت کد #84',
            'Paid': False, 'PosSarfasl': '10200010004',
            'Items': [{'ErpCode': 'P1', 'Amount': 2, 'Price': 66700.0, 'Comment': 'ثبت از سایت'},
                      {'ErpCode': 'SVC', 'Amount': 1, 'Price': 45000.0, 'Comment': 'کرایه پیک'}],
        }
        data.update(extra)
        return data

    def test_unpaid_invoice_is_settled_on_credit_for_exactly_its_total(self):
        info = wire.invoice_body(self.payload())['invoiceinfo'][0]
        self.assertEqual((info['id'], info['type'], info['customererpcode']), ('84', 1, 'CUST='))
        self.assertEqual((info['date'], info['time']), ('2026-10-04', '09:05'))
        self.assertEqual(info['Nesiyeh'], 178400)
        self.assertNotIn('Bank', info)
        self.assertNotIn('BankSarfasl', info)

    def test_paid_invoice_is_settled_by_the_pos_account_not_on_credit(self):
        info = wire.invoice_body(self.payload(Paid=True))['invoiceinfo'][0]
        self.assertEqual((info['Bank'], info['BankSarfasl']), (178400, '10200010004'))
        self.assertNotIn('Nesiyeh', info)

    def test_lines_are_a_list_with_zero_tax_and_integer_numbers(self):
        detail = wire.invoice_body(self.payload())['invoiceinfo'][0]['detailinfo']
        self.assertEqual([d['ProductErpCode'] for d in detail], ['P1', 'SVC'])
        self.assertEqual([(d['few'], d['price'], d['levy'], d['scot']) for d in detail], [(2, 66700, 0, 0), (1, 45000, 0, 0)])
        self.assertEqual([d['id'] for d in detail], ['1', '2'])
        self.assertIsInstance(detail[0]['few'], int)

    def test_settlement_always_equals_the_sum_of_lines_even_with_fractional_prices(self):
        payload = self.payload(Items=[{'ErpCode': 'P', 'Amount': 3, 'Price': 1000.5, 'Comment': ''}])
        info = wire.invoice_body(payload)['invoiceinfo'][0]
        self.assertEqual(info['Nesiyeh'], 3001.5)

    def test_long_comments_are_truncated(self):
        info = wire.invoice_body(self.payload(Comment='ا' * 2000))['invoiceinfo'][0]
        self.assertEqual(len(info['comment']), wire.COMMENT_MAX_LENGTH)

    def test_customer_name_format_is_name_dash_mobile(self):
        self.assertEqual(wire.customer_display_name('علی', 'احمدی', '09120000001'), 'علی احمدی – 09120000001')
        self.assertEqual(wire.customer_display_name('', '', '09120000001'), '09120000001')

    def test_customer_body_is_a_purchaser_with_the_site_user_id_and_address_parts(self):
        info = wire.customer_body(web_id=7, first_name='علی', last_name='احمدی', phone_number='09120000001', national_code='0012345678',
                                  province='قم', city='قم', address='قم، خیابان', postal_code='1234567890')['custinfo'][0]
        self.assertEqual((info['id'], info['ispurchaser'], info['isseller'], info['custtype']), ('7', True, False, 0))
        self.assertEqual((info['mobile'], info['nationalid'], info['ostan'], info['city'], info['zipcode']),
                         ('09120000001', '0012345678', 'قم', 'قم', '1234567890'))

    def test_customer_update_sends_only_the_given_fields(self):
        info = wire.customer_update_body('ERP=', web_id=7, address='جدید')['custinfo'][0]
        self.assertEqual(info, {'erpcode': 'ERP=', 'id': '7', 'address': 'جدید'})

    def test_persian_and_arabic_digits_are_written_as_latin_because_holoo_stores_them_as_question_marks(self):
        self.assertEqual(wire.holoo_text('پلاک ۱۲ و ٣٤'), 'پلاک 12 و 34')
        self.assertEqual(wire.holoo_text(None), '')
        info = wire.customer_body(web_id=7, first_name='علی', last_name='احمدی', phone_number='۰۹۱۲۰۰۰۰۰۰۱', address='خیابان ۵، پلاک ۱',
                                  postal_code='۱۲۳۴۵۶۷۸۹۰')['custinfo'][0]
        self.assertEqual((info['mobile'], info['address'], info['zipcode']), ('09120000001', 'خیابان 5، پلاک 1', '1234567890'))
        self.assertIn('09120000001', info['name'])
        update = wire.customer_update_body('E=', address='پلاک ۳')['custinfo'][0]
        self.assertEqual(update['address'], 'پلاک 3')
        invoice = wire.invoice_body(self.payload(Comment='کد پستی: ۱۱۱۲۲۲۳۳۳۴', Items=[
            {'ErpCode': 'P', 'Amount': 1, 'Price': 10.0, 'Comment': 'ردیف ۱'}]))['invoiceinfo'][0]
        self.assertEqual(invoice['comment'], 'کد پستی: 1112223334')
        self.assertEqual(invoice['detailinfo'][0]['comment'], 'ردیف 1')

    def test_response_parsing(self):
        ok = wire.parse_response({'Header': 'Invoice', 'Success': {'Id': '1', 'ErpCode': 'E', 'Code': '10'}})
        self.assertEqual((ok['success'], ok['data']['Code']), (True, '10'))
        bad = wire.parse_response({'Failure': {'Id': '1', 'Error': 'نبود موجودی', 'ErrorCode': 28}})
        self.assertEqual((bad['success'], bad['code'], bad['transient']), (False, '28', False))
        self.assertTrue(wire.parse_response({'Failure': {'Error': 'x', 'ErrorCode': 5}})['transient'])
        for junk in (None, '', {}, 'html', {'x': 1}):
            parsed = wire.parse_response(junk)
            self.assertEqual((parsed['success'], parsed['code'], parsed['transient']), (False, 'BAD_RESPONSE', True))

    def test_order_comment_matching_survives_holoos_arabic_letters_and_does_not_confuse_prefixes(self):
        stored = 'فاکتور آزمايشي سفارش آنلاين سايت کد #84 | آدرس'            # هلو ی را عربی ذخیره می‌کند
        self.assertTrue(wire.comment_mentions_order(stored, 84))
        self.assertFalse(wire.comment_mentions_order(stored, 8))
        self.assertFalse(wire.comment_mentions_order('سفارش آنلاین سایت کد #840', 84))
        self.assertFalse(wire.comment_mentions_order('', 84))


class RealClientTests(SimpleTestCase):
    def setUp(self):
        _clear_token_cache()
        self.client = HolooClient(real_config())

    def post(self, *replies):
        """ patch کردن login + post/put/get؛ replies به ترتیب پاسخ‌های POST/PUT """
        stack = mock.patch.multiple('holoo.client.requests', post=mock.DEFAULT, put=mock.DEFAULT, get=mock.DEFAULT)
        patched = stack.start()
        self.addCleanup(stack.stop)
        patched['post'].side_effect = [LOGIN_OK, *replies]
        return patched

    # ---------- مشتری ----------
    def test_new_customer_is_posted_and_its_codes_are_returned(self):
        http = self.post(response({'Header': 'Customer', 'Success': {'Id': '7', 'ErpCode': 'bBAD=', 'Code': '03978', 'BedSarfasl': '1033812'}}))
        result = self.client.insert_person('علی', 'احمدی', '09120000001', '0012345678', 'قم، خیابان', web_id=7, province='قم', city='قم')
        self.assertEqual((result['success'], result['erp_code'], result['code'], result['bed_sarfasl'], result['adopted']),
                         (True, 'bBAD=', '03978', '1033812', False))
        call = http['post'].call_args_list[1]
        self.assertEqual(call.args[0], 'http://holoo.test/api/Customer')
        self.assertTrue(call.kwargs['headers']['Authorization'].startswith('Bearer '))
        self.assertIn('utf-8', call.kwargs['headers']['Content-Type'])
        body = json.loads(call.kwargs['data'].decode('utf-8'))
        self.assertEqual(body['custinfo'][0]['name'], 'علی احمدی – 09120000001')

    def test_a_duplicate_mobile_adopts_the_existing_holoo_customer(self):
        http = self.post(response({'Header': 'Customer', 'Failure': {'Id': '7', 'ExistingCustomerErpCode': 'EXIST=', 'Error': 'موبایل تکراری', 'ErrorCode': 10}}))
        http['get'].return_value = response({'Customer': [{'ErpCode': 'EXIST=', 'Code': '00072', 'BedSarfasl': '1030001'}]})
        result = self.client.insert_person('علی', 'احمدی', '09120000001', '', '', web_id=7)
        self.assertEqual((result['success'], result['erp_code'], result['code'], result['adopted']), (True, 'EXIST=', '00072', True))
        self.assertEqual(http['get'].call_args.kwargs['params'], {'erpcode': 'EXIST='})

    def test_a_duplicate_national_id_is_adopted_too(self):
        http = self.post(response({'Failure': {'ExistingCustomerErpCode': 'EXIST=', 'Error': 'کدملی تکراری', 'ErrorCode': 23}}))
        http['get'].return_value = response({'Customer': [{'ErpCode': 'EXIST=', 'Code': '1', 'BedSarfasl': '2'}]})
        self.assertTrue(self.client.insert_person('الف', 'ب', '09120000002', '0012345678', '', web_id=8)['adopted'])

    def test_the_existing_customer_that_cannot_be_read_back_is_retried_not_adopted(self):
        http = self.post(response({'Failure': {'ExistingCustomerErpCode': 'EXIST=', 'Error': 'x', 'ErrorCode': 10}}))
        http['get'].return_value = response({})
        result = self.client.insert_person('الف', 'ب', '09120000002', '', '', web_id=8)
        self.assertEqual((result['success'], result['transient']), (False, True))

    def test_an_empty_name_is_a_permanent_data_error(self):
        self.post(response({'Failure': {'Id': '7', 'Error': 'نام طرف حساب الزامی است', 'ErrorCode': 101}}))
        result = self.client.insert_person('', '', '', '', '', web_id=7)
        self.assertEqual((result['success'], result['code'], result['transient']), (False, '101', False))

    def test_customer_update_uses_put(self):
        http = self.post()
        http['put'].return_value = response({'Header': 'Customer', 'Success': {'ErpCode': 'E='}})
        result = self.client.update_person('E=', first_name='علی', last_name='احمدی', address='جدید', web_id=7, phone_number='09120000001')
        self.assertTrue(result['success'])
        self.assertEqual(http['put'].call_args.args[0], 'http://holoo.test/api/Customer')
        info = json.loads(http['put'].call_args.kwargs['data'].decode('utf-8'))['custinfo'][0]
        self.assertEqual((info['erpcode'], info['id'], info['address']), ('E=', '7', 'جدید'))

    def test_a_duplicate_client_id_on_a_customer_adopts_the_one_already_registered_for_that_user(self):
        http = self.post(response({'Failure': {'Id': '7', 'Error': 'شناسه سمت کلاینت تکراری است', 'ErrorCode': 102}}))
        http['get'].return_value = response({'Customer': [{'ErpCode': 'MINE=', 'Code': '03978', 'BedSarfasl': '1033812', 'WebId': '7'}]})
        result = self.client.insert_person('علی', 'احمدی', '09120000001', '', '', web_id=7)
        self.assertEqual((result['success'], result['erp_code'], result['adopted']), (True, 'MINE=', True))
        self.assertEqual(http['get'].call_args.kwargs['params'], {'webid': '7'})

    def test_a_duplicate_client_id_without_a_findable_customer_needs_manual_review(self):
        http = self.post(response({'Failure': {'Id': '7', 'Error': 'تکراری', 'ErrorCode': 102}}))
        http['get'].return_value = response({'Customer': []})
        result = self.client.insert_person('علی', 'احمدی', '09120000001', '', '', web_id=7)
        self.assertEqual((result['success'], result['code']), (False, '102'))
        self.assertIn('بررسی دستی', result['message'])

    def test_the_client_id_prefix_separates_ids_of_dev_and_main_databases(self):
        client = HolooClient(real_config(client_id_prefix='DEV-'))
        http = self.post(response({'Success': {'Id': 'DEV-7', 'ErpCode': 'E=', 'Code': '1', 'BedSarfasl': '2'}}),
                         response({'Success': {'Code': '5', 'ErpCode': 'I=', 'SanadCode': '9'}}))
        client.insert_person('علی', 'احمدی', '09120000001', '', '', web_id=7)
        customer = json.loads(http['post'].call_args_list[1].kwargs['data'].decode('utf-8'))
        self.assertEqual(customer['custinfo'][0]['id'], 'DEV-7')
        client.insert_invoice(self.invoice_payload())
        invoice = json.loads(http['post'].call_args_list[2].kwargs['data'].decode('utf-8'))
        self.assertEqual(invoice['invoiceinfo'][0]['id'], 'DEV-84')

    # ---------- فاکتور ----------
    def invoice_payload(self):
        return {'OrderId': 84, 'CustomerErpCode': 'CUST=', 'IssuedAt': datetime(2026, 10, 4, 9, 5), 'Comment': 'سفارش آنلاین سایت کد #84',
                'Paid': True, 'PosSarfasl': '10200010004', 'Items': [{'ErpCode': 'P1', 'Amount': 1, 'Price': 66700.0, 'Comment': ''}]}

    def test_invoice_success_returns_number_erp_code_and_sanad(self):
        self.post(response({'Header': 'Invoice', 'Success': {'Id': '84', 'ErpCode': 'bBADcQ5IdgA=', 'Code': '36707', 'SanadCode': '74090'}}))
        result = self.client.insert_invoice(self.invoice_payload())
        self.assertEqual((result['success'], result['InvoiceCode'], result['ErpCode'], result['SanadCode'], result['adopted']),
                         (True, '36707', 'bBADcQ5IdgA=', '74090', False))

    def test_out_of_stock_error_28_is_reported_as_permanent_with_its_code(self):
        self.post(response({'Header': 'Invoice', 'Failure': {'Id': '84', 'Error': 'کالا فاقد موجودی', 'ErrorCode': 28}}))
        result = self.client.insert_invoice(self.invoice_payload())
        self.assertEqual((result['success'], result['code'], result['transient']), (False, '28', False))

    def test_duplicate_client_id_adopts_the_invoice_already_in_holoo(self):
        http = self.post(response({'Failure': {'Id': '84', 'Error': 'شناسه سمت کلاینت تکراری است', 'ErrorCode': 102}}))
        http['get'].return_value = response({'invoice': [
            {'Code': 36000, 'CustomerErpCode': 'OTHER=', 'comment': 'سفارش آنلاين سايت کد #84', 'ErpCode': 'X', 'SanadCode': 1},
            {'Code': 36707, 'CustomerErpCode': 'CUST=', 'comment': 'سفارش آنلاين سايت کد #84 | آدرس', 'ErpCode': 'bBAD=', 'SanadCode': 74090},
        ]})
        result = self.client.insert_invoice(self.invoice_payload())
        self.assertEqual((result['success'], result['InvoiceCode'], result['ErpCode'], result['adopted']), (True, '36707', 'bBAD=', True))
        params = http['get'].call_args.kwargs['params']
        self.assertEqual((params['type'], params['date.from'], params['date.to']), (2, '2026-10-03', '2026-10-07'))

    def test_duplicate_client_id_prefers_the_invoice_whose_total_matches_and_the_oldest_when_several_match(self):
        http = self.post(response({'Failure': {'Id': '84', 'Error': 'تکراری', 'ErrorCode': 102}}))
        http['get'].return_value = response({'invoice': [
            {'Code': 36709, 'CustomerErpCode': 'CUST=', 'comment': 'سفارش آنلاين سايت کد #84', 'SumPrice': 66700.0, 'ErpCode': 'B'},
            {'Code': 36707, 'CustomerErpCode': 'CUST=', 'comment': 'سفارش آنلاين سايت کد #84', 'SumPrice': 99999.0, 'ErpCode': 'A'},
            {'Code': 36711, 'CustomerErpCode': 'CUST=', 'comment': 'سفارش آنلاين سايت کد #84', 'SumPrice': 66700.0, 'ErpCode': 'C'},
        ]})
        result = self.client.insert_invoice(self.invoice_payload())
        self.assertEqual((result['success'], result['InvoiceCode']), (True, '36709'))        # مبلغ برابر ← قدیمی‌ترین بین آن‌ها

    def test_duplicate_client_id_without_a_findable_invoice_needs_manual_review(self):
        http = self.post(response({'Failure': {'Id': '84', 'Error': 'تکراری', 'ErrorCode': 102}}))
        http['get'].return_value = response({'invoice': []})
        result = self.client.insert_invoice(self.invoice_payload())
        self.assertEqual((result['success'], result['code'], result.get('transient', False)), (False, '102', False))
        self.assertIn('بررسی دستی', result['message'])

    def test_network_errors_login_failures_and_5xx_are_transient(self):
        import requests
        http = self.post(requests.ConnectionError('down'))
        result = self.client.insert_invoice(self.invoice_payload())
        self.assertEqual((result['success'], result['code'], result['transient']), (False, 'NETWORK', True))

        _clear_token_cache()
        self.post(response(None, status=502, text='bad gateway'))
        result = self.client.insert_invoice(self.invoice_payload())
        self.assertEqual((result['code'], result['transient']), ('HTTP_502', True))

    def test_http_4xx_other_than_401_is_permanent(self):
        self.post(response(None, status=404, text='not found'))
        result = self.client.insert_invoice(self.invoice_payload())
        self.assertEqual((result['code'], result['transient']), ('HTTP_404', False))

    def test_an_expired_token_is_refreshed_once(self):
        http = self.post(response(None, status=401, text='expired'), LOGIN_OK,
                         response({'Success': {'Code': '5', 'ErpCode': 'E', 'SanadCode': '9'}}))
        result = self.client.insert_invoice(self.invoice_payload())
        self.assertTrue(result['success'])
        self.assertEqual(http['post'].call_count, 4)                         # login, 401, login, success

    def test_login_failure_is_transient(self):
        bad = response({'Login': {'State': False, 'ErrorCode': 1, 'Error': 'db down'}})
        stack = mock.patch.multiple('holoo.client.requests', post=mock.DEFAULT)
        patched = stack.start()
        self.addCleanup(stack.stop)
        patched['post'].return_value = bad
        result = self.client.insert_invoice(self.invoice_payload())
        self.assertEqual((result['success'], result['code'], result['transient']), (False, 'LOGIN_FAILED', True))

    def test_unrecognised_answers_are_treated_as_transient(self):
        self.post(response({'unexpected': True}))
        result = self.client.insert_invoice(self.invoice_payload())
        self.assertEqual((result['code'], result['transient']), ('BAD_RESPONSE', True))

    # ---------- محافظت‌ها ----------
    def test_real_mode_on_a_non_allow_listed_database_never_touches_the_network(self):
        client = HolooClient(real_config(db='HolooMain'))
        with mock.patch('holoo.client.requests') as http:
            for call in (lambda: client.insert_invoice(self.invoice_payload()), lambda: client.insert_person('a', 'b', '09120000001', ''),
                         lambda: client.update_person('E=', first_name='a')):
                result = call()
                self.assertEqual((result['success'], result.get('code')), (False, 'WRITE_DISABLED'))
            # دروازه‌ی داخلی هم مستقل از متدهای عمومی همین را رد می‌کند
            self.assertEqual(client._real_write('POST', 'Customer', {})['code'], 'WRITE_DISABLED')
        self.assertFalse(http.post.called or http.put.called or http.request.called)

    def test_separate_payment_receipts_are_not_sent_in_real_mode(self):
        with mock.patch('holoo.client.requests') as http:
            result = self.client.register_payment('36707', 66700)
        self.assertEqual((result['success'], result['code'], result['transient']), (False, 'NOT_IMPLEMENTED', False))
        self.assertFalse(http.post.called)


class TaskBase(TestCase):
    def setUp(self):
        self.addCleanup(cache.delete, SiteSettings.CACHE_KEY)
        province = Province.objects.create(name='استان واقعی تست هلو')
        self.city = City.objects.create(province=province, name='شهر واقعی تست هلو')
        self.user = CustomUser.objects.create_user(phone_number='09120000800', first_name='علی', last_name='احمدی', national_code='0012345678')
        Address.objects.create(user=self.user, title='منزل', receiver_first_name='ع', receiver_last_name='ا', receiver_phone='09121112233',
                               city=self.city, postal_code='1234567890', address='خیابان تست')
        category = Category.objects.create(name='واقعی', slug='real-write-cat')
        self.product = Product.objects.create(name='کالا', slug='real-write-p', erp_code='ERP-RW-1', category=category, price=100000, stock=50)
        for key in ('lock:holoo:invoice:%s',):
            pass

    def make_order(self, paid=False, method='check', **extra):
        order = Order.objects.create(user=self.user, first_name='علی', last_name='احمدی', phone='09120000800', address='خیابان تست',
                                     payment_method=method, total_price=200000, approved_at=timezone.now(), **extra)
        OrderItem.objects.create(order=order, product=self.product, price=100000, quantity=2)
        with stock.transaction.atomic():
            stock.reserve_for_order(order.id, {self.product.pk: 2})
        if paid:
            Transaction.objects.create(user=self.user, order=order, amount=200000, status='success', authority=f'AUTH-RW-{order.id}')
        cache.delete('lock:holoo:invoice:%s' % order.id)
        return order


class CustomerTaskTests(TaskBase):
    def run_sync(self, **result):
        with mock.patch('holoo.tasks.HolooClient') as client:
            client.return_value.insert_person.return_value = result
            client.return_value.update_person.return_value = result
            outcome = sync_user_to_holoo.run(self.user.id)
        return outcome, client.return_value

    def test_new_customer_codes_are_stored_on_the_user(self):
        outcome, client = self.run_sync(success=True, erp_code='bBAD=', code='03978', bed_sarfasl='1033812')
        self.assertEqual(outcome, 'Sync Success')
        self.user.refresh_from_db()
        self.assertEqual((self.user.erp_code, self.user.holoo_customer_code, self.user.holoo_bed_sarfasl), ('bBAD=', '03978', '1033812'))
        kwargs = client.insert_person.call_args.kwargs
        self.assertEqual((kwargs['web_id'], kwargs['province'], kwargs['city'], kwargs['postal_code']), (self.user.id, 'استان واقعی تست هلو', 'شهر واقعی تست هلو', '1234567890'))

    def test_existing_customer_is_updated_not_recreated(self):
        CustomUser.objects.filter(pk=self.user.pk).update(erp_code='OLD=')
        self.user.refresh_from_db()
        outcome, client = self.run_sync(success=True)
        client.insert_person.assert_not_called()
        self.assertEqual(client.update_person.call_args.kwargs['web_id'], self.user.id)

    def test_permanent_error_stops_without_retry_and_keeps_the_message(self):
        with mock.patch('holoo.tasks.sync_user_to_holoo.retry', side_effect=RuntimeError('retried')):
            outcome, _ = self.run_sync(success=False, code='101', message='نام الزامی است', transient=False)
        self.assertEqual(outcome, 'Fatal Data Error - No Retry')
        self.user.refresh_from_db()
        self.assertEqual((self.user.last_sync_error, self.user.retry_count, self.user.erp_code), ('نام الزامی است', 1, None))

    def test_transient_error_retries(self):
        with mock.patch('holoo.tasks.sync_user_to_holoo.retry', side_effect=RuntimeError('retried')):
            with self.assertRaises(RuntimeError):
                self.run_sync(success=False, code='NETWORK', message='down', transient=True)


class InvoiceTaskTests(TaskBase):
    OK = {'success': True, 'InvoiceCode': '36707', 'ErpCode': 'bBAD=', 'SanadCode': '74090', 'adopted': False}

    def send(self, order, **result):
        with mock.patch('holoo.client.HolooClient.insert_invoice') as insert:
            insert.return_value = result or self.OK
            outcome = send_order_to_holoo.run(order.id)
        return outcome, insert

    def test_paid_order_is_sent_settled_and_its_sanad_becomes_the_receipt(self):
        CustomUser.objects.filter(pk=self.user.pk).update(erp_code='CUST=')
        SiteSettings.objects.update_or_create(pk=1, defaults={'holoo_pos_sarfasl': '10200010004'})
        cache.delete(SiteSettings.CACHE_KEY)
        order = self.make_order(paid=True, method='cash')
        outcome, insert = self.send(order)
        payload = insert.call_args[0][0]
        self.assertEqual((payload['OrderId'], payload['Paid'], payload['PosSarfasl'], payload['CustomerErpCode']),
                         (order.id, True, '10200010004', 'CUST='))
        order.refresh_from_db()
        self.assertEqual((order.holoo_invoice_id, order.holoo_invoice_erp_code, order.holoo_receipt_id, order.status),
                         ('36707', 'bBAD=', '74090', 'registered'))
        self.assertFalse(order.holoo_needs_attention)

    def test_cheque_order_is_sent_on_credit_with_no_receipt(self):
        CustomUser.objects.filter(pk=self.user.pk).update(erp_code='CUST=')
        order = self.make_order(paid=False, method='check')
        outcome, insert = self.send(order)
        self.assertFalse(insert.call_args[0][0]['Paid'])
        order.refresh_from_db()
        self.assertEqual((order.holoo_invoice_id, order.holoo_receipt_id), ('36707', None))

    def test_a_customer_missing_in_holoo_is_created_first_then_the_invoice_is_sent(self):
        order = self.make_order(paid=False)
        with mock.patch('holoo.client.HolooClient.insert_person') as person:
            person.return_value = {'success': True, 'erp_code': 'NEW=', 'code': '03978', 'bed_sarfasl': '1033812'}
            outcome, insert = self.send(order)
        person.assert_called_once()
        self.assertEqual(insert.call_args[0][0]['CustomerErpCode'], 'NEW=')
        self.user.refresh_from_db()
        self.assertEqual(self.user.erp_code, 'NEW=')

    def test_a_permanent_customer_error_flags_the_order_and_alerts_once_without_retry(self):
        order = self.make_order()
        with mock.patch('holoo.client.HolooClient.insert_person') as person, mock.patch('notifications.service.notify_admin') as alert, \
             mock.patch('holoo.tasks.send_order_to_holoo.retry', side_effect=RuntimeError('retried')):
            person.return_value = {'success': False, 'code': '101', 'message': 'نام الزامی است', 'transient': False}
            outcome = send_order_to_holoo.run(order.id)
            again = send_order_to_holoo.run(order.id)
        self.assertTrue(outcome.startswith('Needs attention'))
        order.refresh_from_db()
        self.assertTrue(order.holoo_needs_attention)
        self.assertIn('نام الزامی است', order.holoo_last_error)
        self.assertEqual(alert.call_count, 1)                              # هشدار فقط بار اول
        self.assertFalse(order.holoo_invoice_id)

    def test_a_transient_customer_error_retries(self):
        order = self.make_order()
        with mock.patch('holoo.client.HolooClient.insert_person') as person, \
             mock.patch('holoo.tasks.send_order_to_holoo.retry', side_effect=RuntimeError('retried')):
            person.return_value = {'success': False, 'code': 'NETWORK', 'message': 'down', 'transient': True}
            with self.assertRaises(RuntimeError):
                send_order_to_holoo.run(order.id)
        self.assertFalse(Order.objects.get(pk=order.pk).holoo_needs_attention)

    def test_a_permanent_invoice_error_flags_the_order_and_does_not_retry(self):
        CustomUser.objects.filter(pk=self.user.pk).update(erp_code='CUST=')
        order = self.make_order()
        with mock.patch('notifications.service.notify_admin') as alert, \
             mock.patch('holoo.tasks.send_order_to_holoo.retry', side_effect=RuntimeError('retried')):
            outcome, _ = self.send(order, success=False, code='15', message='کد کالا معتبر نمیباشد', transient=False)
        self.assertTrue(outcome.startswith('Needs attention'))
        order.refresh_from_db()
        self.assertEqual((order.holoo_needs_attention, order.status, order.holoo_invoice_id), (True, 'pending', None))
        self.assertIn('15', order.holoo_last_error)
        alert.assert_called_once()
        self.assertEqual(StockReservation.objects.get(order_id=order.id).state, 'held')        # رزرو سر جایش می‌ماند

    def test_a_transient_invoice_error_retries(self):
        CustomUser.objects.filter(pk=self.user.pk).update(erp_code='CUST=')
        order = self.make_order()
        with mock.patch('holoo.tasks.send_order_to_holoo.retry', side_effect=RuntimeError('retried')):
            with self.assertRaises(RuntimeError):
                self.send(order, success=False, code='NETWORK', message='down', transient=True)
        self.assertFalse(Order.objects.get(pk=order.pk).holoo_needs_attention)

    def test_reconcile_skips_orders_that_need_attention(self):
        CustomUser.objects.filter(pk=self.user.pk).update(erp_code='CUST=')
        order = self.make_order()
        Order.objects.filter(pk=order.pk).update(created_at=timezone.now() - timedelta(hours=2))
        with mock.patch('holoo.tasks.send_order_to_holoo.delay') as task:
            reconcile_holoo_orders()
            task.assert_called_once_with(order.id)
        Order.objects.filter(pk=order.pk).update(holoo_needs_attention=True)
        with mock.patch('holoo.tasks.send_order_to_holoo.delay') as task:
            reconcile_holoo_orders()
            task.assert_not_called()

    def test_a_successful_retry_clears_the_attention_flag(self):
        CustomUser.objects.filter(pk=self.user.pk).update(erp_code='CUST=')
        order = self.make_order()
        Order.objects.filter(pk=order.pk).update(holoo_needs_attention=True, holoo_last_error='قبلی')
        self.send(order)
        order.refresh_from_db()
        self.assertEqual((order.holoo_needs_attention, order.holoo_last_error), (False, ''))


class AdminRetryActionTests(TaskBase):
    def test_retry_action_requeues_only_approved_orders_without_an_invoice(self):
        admin_user = CustomUser.objects.create_superuser(phone_number='09120000801', password='x')
        self.client.force_login(admin_user)
        flagged = self.make_order()
        Order.objects.filter(pk=flagged.pk).update(holoo_needs_attention=True, holoo_last_error='خطا')
        invoiced = self.make_order(holoo_invoice_id='111')
        unapproved = self.make_order()
        Order.objects.filter(pk=unapproved.pk).update(approved_at=None)
        from django.urls import reverse
        with mock.patch('holoo.tasks.send_order_to_holoo.delay') as task:
            response = self.client.post(reverse('admin:orders_order_changelist'), {
                'action': 'retry_holoo_registration', '_selected_action': [flagged.pk, invoiced.pk, unapproved.pk]}, follow=True)
        task.assert_called_once_with(flagged.pk)
        flagged.refresh_from_db()
        self.assertEqual((flagged.holoo_needs_attention, flagged.holoo_last_error), (False, ''))
        self.assertContains(response, '1 سفارش دوباره')
        self.assertContains(response, '2 سفارش رد شد')


class SettingsDefaultsTests(TestCase):
    def test_site_settings_defaults_point_at_real_holoo_values(self):
        self.addCleanup(cache.delete, SiteSettings.CACHE_KEY)
        settings_obj = SiteSettings.load()
        self.assertEqual(settings_obj.holoo_pos_sarfasl, '10200010004')
        self.assertEqual(settings_obj.shipping_erp_code, 'bBALNA1mckd7Zh4O')       # «سرويس»، نه '999999'
