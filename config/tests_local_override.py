"""
تنظیمات مخصوص هر ماشین (config/local_override.py): فایل اختیاری local_settings.py داخل فضای نام settings اجرا می‌شود.
"""
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from config.local_override import apply_local_settings


class ApplyLocalSettingsTests(SimpleTestCase):
    def run_file(self, text, namespace):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'local_settings.py'
            path.write_text(text, encoding='utf-8')
            return apply_local_settings(namespace, path)

    def test_missing_file_is_a_silent_no_op(self):
        namespace = {'A': 1}
        self.assertFalse(apply_local_settings(namespace, Path(tempfile.gettempdir()) / 'no-such-local-settings.py'))
        self.assertEqual(namespace, {'A': 1})

    def test_values_can_be_replaced_and_dicts_edited_in_place(self):
        namespace = {'CSRF_TRUSTED_ORIGINS': [], 'DATABASES': {'default': {'NAME': 'db', 'PASSWORD': 'dev', 'PORT': ''}}}
        applied = self.run_file(
            "CSRF_TRUSTED_ORIGINS = ['https://example.ir']\nDATABASES['default'].update({'PASSWORD': 'server', 'PORT': '1433'})\n", namespace)
        self.assertTrue(applied)
        self.assertEqual(namespace['CSRF_TRUSTED_ORIGINS'], ['https://example.ir'])
        self.assertEqual(namespace['DATABASES']['default'], {'NAME': 'db', 'PASSWORD': 'server', 'PORT': '1433'})

    def test_file_saved_with_a_bom_and_persian_comments_still_loads(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'local_settings.py'
            path.write_bytes('\ufeff# توضیح فارسی\nDEBUG = False\n'.encode('utf-8'))
            namespace = {'DEBUG': True}
            self.assertTrue(apply_local_settings(namespace, path))
        self.assertFalse(namespace['DEBUG'])

    def test_a_broken_file_fails_loudly_instead_of_booting_with_wrong_settings(self):
        with self.assertRaises(SyntaxError):
            self.run_file('DEBUG = (\n', {})


class WiringTests(SimpleTestCase):
    def test_settings_apply_the_local_file_last_and_git_ignores_it(self):
        source = (Path(settings.BASE_DIR) / 'config' / 'settings.py').read_text(encoding='utf-8')
        self.assertTrue(source.rstrip().endswith("apply_local_settings(globals(), BASE_DIR / 'config' / 'local_settings.py')"))
        ignore = (Path(settings.BASE_DIR) / '.gitignore').read_text(encoding='utf-8')
        self.assertIn('config/local_settings.py', ignore)

    def test_the_example_file_is_valid_against_the_real_settings_shape(self):
        example = Path(settings.BASE_DIR) / 'config' / 'local_settings.example.py'
        namespace = {'DATABASES': {'default': {'NAME': 'x', 'PASSWORD': 'dev', 'PORT': ''}}}
        self.assertTrue(apply_local_settings(namespace, example))
        self.assertEqual(namespace['DATABASES']['default']['PORT'], '1433')
        self.assertTrue(namespace['CSRF_TRUSTED_ORIGINS'])
