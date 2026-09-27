"""
تست‌های Part B.2: فیلدست «تنظیمات و قوانین مرجوعی کالا» در SiteSettingsAdmin.
"""

from django.test import TestCase
from django.urls import reverse

from accounts.models import CustomUser
from products.models import SiteSettings


class ReturnSettingsAdminTests(TestCase):
    FIELDS = ('return_period_days', 'return_period_unit', 'return_policy_html')

    def setUp(self):
        SiteSettings.load().save()
        self.admin = CustomUser.objects.create_superuser(phone_number='09120005099')
        self.client.force_login(self.admin)

    def test_defaults_match_the_model(self):
        settings_obj = SiteSettings.load()
        self.assertEqual(settings_obj.return_period_days, 7)
        self.assertEqual(settings_obj.return_period_unit, SiteSettings.RETURN_PERIOD_UNIT_WORKING_DAYS)
        self.assertEqual(settings_obj.return_policy_html, '')

    def test_admin_shows_the_return_policy_section(self):
        response = self.client.get(reverse('admin:products_sitesettings_change', args=[1]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'تنظیمات و قوانین مرجوعی کالا')
        for field in self.FIELDS:
            with self.subTest(field=field):
                self.assertContains(response, f'name="{field}"')

    def _post_all(self, **overrides):
        """ فرم ادمین تنظیمات سایت با همه‌ی فیلدهای فعلی + تغییرات - هم‌الگوی سایر تست‌های SiteSettingsAdmin """
        from django.db.models.fields.files import FieldFile
        from django.forms.models import model_to_dict
        data = model_to_dict(SiteSettings.load())
        data.update(overrides)
        data = {
            k: v for k, v in data.items()
            if v is not None and v is not False and not (isinstance(v, FieldFile) and not v)
        }
        return self.client.post(reverse('admin:products_sitesettings_change', args=[1]), data)

    def test_saving_and_loading_return_period_and_policy_text(self):
        response = self._post_all(
            return_period_days=14, return_period_unit=SiteSettings.RETURN_PERIOD_UNIT_CALENDAR_DAYS,
            return_policy_html='<p>مشتری تا ۱۴ روز تقویمی فرصت دارد.</p>',
        )
        self.assertEqual(response.status_code, 302)
        for stored in (SiteSettings.load(), SiteSettings.cached()):   # کش هم بلافاصله تازه شود
            self.assertEqual(stored.return_period_days, 14)
            self.assertEqual(stored.return_period_unit, SiteSettings.RETURN_PERIOD_UNIT_CALENDAR_DAYS)
            self.assertIn('۱۴ روز تقویمی', stored.return_policy_html)

    def test_zero_return_period_days_is_rejected(self):
        response = self._post_all(return_period_days=0)
        self.assertEqual(response.status_code, 200)   # فرم با خطا برمی‌گردد، ذخیره نمی‌شود
        self.assertEqual(SiteSettings.load().return_period_days, 7)

    def test_return_policy_html_can_be_left_blank(self):
        response = self._post_all(return_policy_html='')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SiteSettings.load().return_policy_html, '')
