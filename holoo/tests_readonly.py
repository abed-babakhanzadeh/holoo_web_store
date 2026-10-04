"""
فاز ۰ و ۱ اتصال هلو: تنظیمات امن، تفکیک پرچم خواندن/نوشتن (هیچ درخواست نوشتنی به هلو نمی‌رود)،
فیلتر کالای خدماتی در سینک، و تحلیل فقط‌خواندنیِ داده‌ی کالاها.
"""
import tempfile
from pathlib import Path
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from holoo.analysis import analyze_products, compare_with_site
from holoo.client import HolooClient, _clear_token_cache
from holoo.conf import get_config, read_env_file
from holoo.tasks import classify_item, is_service_item, send_order_to_holoo, sync_products_from_holoo
from products.models import Category, Product


class FakeSettings:
    HOLOO_API_URL = 'http://legacy.example/api'
    HOLOO_USERNAME = 'legacy-user'
    HOLOO_PASSWORD = 'bGVnYWN5'
    HOLOO_DB_NAME = 'LegacyDb'
    HOLOO_PRODUCTS_MOCK_MODE = False


def cfg(env=None, file_text=None, settings_obj=FakeSettings):
    """ ساخت تنظیمات با env/.env/settings ساختگی؛ هرگز به محیط واقعی دست نمی‌زند """
    if file_text is None:
        return get_config(env=env or {}, env_file='/nonexistent/.env', settings_obj=settings_obj)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / '.env'
        path.write_text(file_text, encoding='utf-8')
        return get_config(env=env or {}, env_file=path, settings_obj=settings_obj)


class ConfigTests(SimpleTestCase):
    def test_defaults_read_real_write_mock(self):
        config = cfg()
        self.assertEqual((config.read_mode, config.write_mode), ('real', 'mock'))

    def test_legacy_settings_are_the_fallback(self):
        config = cfg()
        self.assertEqual((config.base_url, config.username, config.db_name), ('http://legacy.example/api', 'legacy-user', 'LegacyDb'))

    def test_env_beats_env_file_beats_legacy_settings(self):
        config = cfg(env={'HOLOO_DB_NAME': 'FromEnv'}, file_text='HOLOO_DB_NAME=FromFile\nHOLOO_USERNAME=file-user\n')
        self.assertEqual(config.db_name, 'FromEnv')
        self.assertEqual(config.username, 'file-user')
        self.assertEqual(config.password, 'bGVnYWN5')

    def test_env_file_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / '.env'
            path.write_text('# comment\n\nA=1\nexport B = "two words"\nC=\'x\'\nbroken line\n', encoding='utf-8')
            self.assertEqual(read_env_file(path), {'A': '1', 'B': 'two words', 'C': 'x'})
        self.assertEqual(read_env_file('/nonexistent/.env'), {})

    def test_legacy_products_mock_flag_still_selects_read_mock(self):
        class Legacy(FakeSettings):
            HOLOO_PRODUCTS_MOCK_MODE = True
        self.assertEqual(cfg(settings_obj=Legacy).read_mode, 'mock')
        self.assertEqual(cfg(env={'HOLOO_READ_MODE': 'real'}, settings_obj=Legacy).read_mode, 'real')

    def test_real_write_mode_is_refused_and_fails_closed(self):
        for value in ('real', 'REAL', 'true', 'typo', '1'):
            with self.subTest(value=value):
                config = cfg(env={'HOLOO_WRITE_MODE': value})
                self.assertEqual(config.write_mode, 'disabled')
                self.assertTrue(config.warnings)

    def test_explicit_write_modes(self):
        self.assertEqual(cfg(env={'HOLOO_WRITE_MODE': 'mock'}).write_mode, 'mock')
        self.assertEqual(cfg(env={'HOLOO_WRITE_MODE': 'disabled'}).write_mode, 'disabled')

    def test_legacy_general_mock_flag_cannot_enable_real_writes(self):
        class Legacy(FakeSettings):
            HOLOO_MOCK_MODE = False
        self.assertEqual(cfg(settings_obj=Legacy).write_mode, 'mock')

    def test_password_is_never_exposed(self):
        config = cfg(env={'HOLOO_PASSWORD': 'SuperSecret=='})
        self.assertNotIn('SuperSecret', repr(config))
        self.assertNotIn('SuperSecret', str(config.masked()))
        self.assertEqual(config.masked()['password'], '***')

    def test_problems_report_missing_values_without_secrets(self):
        class Empty:
            pass
        config = cfg(settings_obj=Empty)
        self.assertEqual(len(config.problems()), 3)       # username/password/db (آدرس پیش‌فرض دارد)
        self.assertEqual(cfg(env={'HOLOO_READ_MODE': 'mock'}, settings_obj=Empty).problems(), [])


class NoWriteRequestTests(SimpleTestCase):
    """ تضمین ساختاری: در هیچ حالتی از نوشتن، هیچ درخواست HTTP (جز لاگین خواندن) فرستاده نمی‌شود """

    def make_client(self, write_mode):
        return HolooClient(cfg(env={'HOLOO_WRITE_MODE': write_mode}))

    def calls(self, client):
        return [
            lambda: client.insert_person('علی', 'رضایی', '09120000001', '1234567890', 'تهران'),
            lambda: client.update_person('ERP', first_name='علی'),
            lambda: client.insert_invoice({'Items': []}),
            lambda: client.register_payment('INV_1', 1000),
        ]

    def test_disabled_mode_refuses_every_write_without_any_http(self):
        client = self.make_client('disabled')
        with mock.patch('holoo.client.requests') as http:
            for call in self.calls(client):
                result = call()
                self.assertFalse(result['success'])
                self.assertTrue(result['disabled'])
                self.assertEqual(result['code'], 'WRITE_DISABLED')
        self.assertFalse(http.post.called or http.put.called or http.get.called or http.request.called)

    def test_mock_mode_simulates_locally_without_any_http(self):
        client = self.make_client('mock')
        with mock.patch('holoo.client.requests') as http, mock.patch('holoo.client.time.sleep'):
            for call in self.calls(client):
                self.assertTrue(call()['success'])
        self.assertFalse(http.post.called or http.put.called or http.get.called or http.request.called)

    def test_typo_in_write_mode_cannot_reach_holoo(self):
        client = self.make_client('real')
        with mock.patch('holoo.client.requests') as http:
            self.assertTrue(client.insert_invoice({})['disabled'])
        self.assertFalse(http.post.called)


class WriteTasksSkipWhenDisabledTests(TestCase):
    def test_tasks_do_not_run_or_retry_when_writes_are_disabled(self):
        with mock.patch.dict('os.environ', {'HOLOO_WRITE_MODE': 'disabled'}), \
             mock.patch('holoo.tasks.HolooClient') as client_cls:
            result = send_order_to_holoo.apply(args=[999999]).result
        self.assertEqual(result, 'Skipped (holoo writes disabled)')
        client_cls.assert_not_called()


class ReadPathTests(SimpleTestCase):
    def setUp(self):
        _clear_token_cache()

    def login_response(self, token='x.e30.y'):
        response = mock.Mock(status_code=200)
        response.json.return_value = {'Login': {'State': 'True', 'Token': 'Bearer ' + token, 'ErrorCode': 0, 'Error': ''}}
        return response

    def test_login_posts_the_documented_body_and_caches_the_token_per_database(self):
        client = HolooClient(cfg(env={'HOLOO_DB_NAME': 'Holoo2', 'HOLOO_PASSWORD': 'MQ=='}))
        with mock.patch('holoo.client.requests.post', return_value=self.login_response()) as post:
            self.assertEqual(client.login()['status'], 'success')
            client.login()                                                       # دومی از کش
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs['json'], {'userinfo': {'username': 'legacy-user', 'userpass': 'MQ==', 'dbname': 'Holoo2'}})
        # دیتابیس دیگر = توکن دیگر
        other = HolooClient(cfg(env={'HOLOO_DB_NAME': 'HolooOther'}))
        with mock.patch('holoo.client.requests.post', return_value=self.login_response()) as post:
            other.login()
        self.assertEqual(post.call_count, 1)

    def test_login_failure_reports_error_code_and_message(self):
        bad = mock.Mock(status_code=200)
        bad.json.return_value = {'Login': {'State': False, 'ErrorCode': 1, 'Error': 'bad credentials'}}
        client = HolooClient(cfg())
        with mock.patch('holoo.client.requests.post', return_value=bad):
            result = client.login()
        self.assertEqual((result['status'], result['code'], result['message']), ('error', 1, 'bad credentials'))

    def test_get_json_uses_get_only_with_bearer_token(self):
        client = HolooClient(cfg())
        get_response = mock.Mock(status_code=200)
        get_response.json.return_value = {'version': '1.7.33'}
        with mock.patch('holoo.client.requests.post', return_value=self.login_response()), \
             mock.patch('holoo.client.requests.get', return_value=get_response) as get:
            self.assertEqual(client.get_json('/Version'), {'version': '1.7.33'})
        self.assertEqual(get.call_args.args[0], 'http://legacy.example/api/Version')
        self.assertTrue(get.call_args.kwargs['headers']['Authorization'].startswith('Bearer '))

    def test_get_json_is_inert_in_read_mock_mode(self):
        client = HolooClient(cfg(env={'HOLOO_READ_MODE': 'mock'}))
        with mock.patch('holoo.client.requests') as http:
            self.assertIsNone(client.get_json('Version'))
        self.assertFalse(http.get.called or http.post.called)


def item(erp, **extra):
    base = {
        'ErpCode': erp, 'Name': f'کالا {erp}', 'Code': f'C-{erp}', 'Few': 5, 'FewSpd': 5, 'FewTak': 5,
        'SellPrice': 100000, 'SellPrice2': 90000, 'SellPrice3': 0, 'SellPrice4': 0, 'SellPrice5': 0,
        'SellPrice6': 0, 'SellPrice7': 0, 'SellPrice8': 0, 'SellPrice9': 0, 'SellPrice10': 0,
        'IsActive': True, 'service': False,
    }
    base.update(extra)
    return base


class ClassifyTests(SimpleTestCase):
    def test_service_flag_variants(self):
        self.assertTrue(is_service_item({'service': True}))
        self.assertTrue(is_service_item({'service': 'true'}))
        self.assertFalse(is_service_item({'service': False}))
        self.assertFalse(is_service_item({'service': 'false'}))
        self.assertFalse(is_service_item({}))

    def test_verdicts(self):
        self.assertEqual(classify_item(item('A')), 'ok')
        self.assertEqual(classify_item(item('A', IsActive=False)), 'inactive')
        self.assertEqual(classify_item(item('A', service=True)), 'service')
        self.assertEqual(classify_item(item('A', Name='ضدآفتاب مصرف کننده 699/000')), 'excluded_name')
        self.assertEqual(classify_item(item('')), 'no_erp')
        self.assertEqual(classify_item(item('A', IsActive=False, service=True)), 'inactive')


class ServiceItemsStayOutOfTheStoreTests(TestCase):
    def setUp(self):
        cache.delete('lock:holoo:product_sync')

    def run_sync(self, items):
        with mock.patch('holoo.client.HolooClient.get_product_count', return_value=len(items)), \
             mock.patch('holoo.client.HolooClient.get_products', return_value={'product': items}):
            return sync_products_from_holoo()

    def test_service_item_is_never_created(self):
        self.run_sync([item('ERP-SVC-1', service=True, Few=1000000000, Name='سرويس'), item('ERP-REAL-1')])
        self.assertFalse(Product.objects.filter(erp_code='ERP-SVC-1').exists())
        self.assertTrue(Product.objects.filter(erp_code='ERP-REAL-1').exists())

    def test_existing_service_product_is_removed_by_the_cleanup(self):
        category = Category.objects.create(name='تست خدمات', slug='svc-test-cat')
        Product.objects.create(name='سرويس', slug='svc-test-product', erp_code='ERP-SVC-2', category=category, price=24500, stock=1000000000)
        self.run_sync([item('ERP-SVC-2', service=True), item('ERP-REAL-2')])
        self.assertFalse(Product.objects.filter(erp_code='ERP-SVC-2').exists())

    def test_result_line_counts_services(self):
        result = self.run_sync([item('ERP-SVC-3', service=True), item('ERP-REAL-3')])
        self.assertIn('services=1', result)
        self.assertIn('fetched=1', result)


class AnalysisTests(SimpleTestCase):
    def sample(self):
        return [
            item('A', Few=3, FewSpd=2, FewTak=3, SellPrice2=1000.5, Code='X1'),
            item('B', Few=0, FewSpd=0, FewTak=0, Code='X1'),
            item('C', IsActive=False, Few=4),
            item('D', service=True, Few=1000000000, Code=None, Name='سرويس'),
            item('E', Name='ضدآفتاب 699/000'),
            item('F', SellPrice=0, Few=5),
        ]

    def test_counts_and_stock_semantics(self):
        report = analyze_products(self.sample())
        self.assertEqual(report['verdicts'], {'ok': 3, 'inactive': 1, 'service': 1, 'excluded_name': 1})
        self.assertEqual(report['importable'], 3)
        self.assertEqual(report['stock']['few_positive'], 2)
        self.assertEqual(report['stock']['few_zero'], 1)
        self.assertEqual(report['stock']['differs_from']['FewSpd']['count'], 1)
        self.assertEqual(report['stock']['differs_from']['FewTak']['count'], 0)
        self.assertEqual(report['inactive_with_stock'], 1)

    def test_prices_identity_and_services(self):
        report = analyze_products(self.sample())
        self.assertEqual(report['prices']['sell_zero_among_importable'], 1)
        self.assertEqual(report['prices']['fractional']['SellPrice2'], 1)
        self.assertEqual(report['identity']['duplicate_codes'], {'X1': 2})
        self.assertEqual(report['identity']['without_code'], 1)
        self.assertEqual([s['ErpCode'] for s in report['services']], ['D'])

    def test_compare_with_site_mirrors_the_cleanup_rule(self):
        compare = compare_with_site(self.sample(), ['A', 'C', 'D', 'GONE', None])
        self.assertEqual(compare['site_total'], 4)
        self.assertEqual(compare['matched'], 1)                                    # A
        self.assertEqual(compare['new_in_holoo'], 2)                               # B, F
        self.assertEqual(compare['would_be_deleted'], 3)                           # C(inactive) D(service) GONE
        self.assertEqual(compare['would_be_deleted_but_exist_in_holoo_filtered'], 2)
        self.assertEqual(compare['would_be_deleted_missing_from_holoo'], 1)


class CheckHolooCommandTests(TestCase):
    def test_command_refuses_in_read_mock_mode(self):
        from django.core.management.base import CommandError
        with mock.patch.dict('os.environ', {'HOLOO_READ_MODE': 'mock'}), self.assertRaises(CommandError):
            call_command('check_holoo')

    def test_command_runs_read_only_against_a_fake_server(self):
        from io import StringIO
        responses = {'Version': {'version': '1.7.33'}, 'Settings': {'CurrencyUnit': [{'string': 'تومان'}]}}
        out = StringIO()
        with mock.patch.dict('os.environ', {'HOLOO_READ_MODE': 'real', 'HOLOO_USERNAME': 'u', 'HOLOO_PASSWORD': 'p', 'HOLOO_DB_NAME': 'Holoo2'}), \
             mock.patch('holoo.client.HolooClient.login', return_value={'status': 'success'}), \
             mock.patch('holoo.client.HolooClient.get_json', side_effect=lambda path, params=None: responses[path]), \
             mock.patch('holoo.client.HolooClient.get_product_count', return_value=2), \
             mock.patch('holoo.client.HolooClient.get_products', return_value={'product': [item('CMD-1'), item('CMD-2', service=True)]}), \
             mock.patch('holoo.client.requests') as http:
            call_command('check_holoo', stdout=out)
        text = out.getvalue()
        self.assertIn('تومان', text)
        self.assertIn('"importable": 1', text)
        self.assertIn('password: ***', text)                                          # رمز چاپ نمی‌شود
        self.assertFalse(http.post.called or http.put.called)
