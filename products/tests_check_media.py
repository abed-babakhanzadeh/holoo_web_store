from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from products.models import Brand


class CheckMediaCommandTests(TestCase):
    def run_cmd(self):
        out = StringIO()
        call_command('check_media', stdout=out)
        return out.getvalue()

    def test_reports_a_file_that_is_in_the_database_but_not_on_disk(self):
        Brand.objects.create(name='برند گمشده', slug='lost-brand', logo='brands/this-file-does-not-exist-xyz.png')
        text = self.run_cmd()
        self.assertIn('products.Brand.logo: 1 از 1 فایل گم شده', text)
        self.assertIn('this-file-does-not-exist-xyz.png', text)

    def test_says_all_good_when_nothing_is_missing(self):
        self.assertIn('0 فایل گم‌شده', self.run_cmd())
