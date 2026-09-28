"""
تست‌های Loyalty Phase 2A: مرز فعال‌سازی باشگاه مشتریان بدون بک‌فیل
(SiteSettings.loyalty_activated_at).

این فیلد در این زیرفاز صرفاً یک محل ذخیره‌ی خالی است - هیچ منطق Earn/Reverse به آن وصل نیست؛
تست‌های این فایل همین «بی‌اثر بودن فعلی» را هم صریحاً اثبات می‌کنند تا اگر فازهای بعدی سهواً
پیامدی جانبی (مثل بک‌فیل خودکار) به آن اضافه کردند، این تست‌ها قرمز شوند.
"""

from django.contrib.admin.sites import site as admin_site
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomUser
from loyalty.models import LoyaltyAccount, LoyaltyTransaction
from orders.models import Order
from payments.models import Transaction
from products.models import SiteSettings


class LoyaltyActivationFieldTests(TestCase):
    def setUp(self):
        SiteSettings.load().save()
        self.addCleanup(self._reset)

    def _reset(self):
        obj = SiteSettings.load()
        obj.loyalty_activated_at = None
        obj.save()

    def test_default_value_is_null(self):
        settings_obj = SiteSettings.load()
        self.assertIsNone(settings_obj.loyalty_activated_at)

    def test_field_is_optional_on_full_clean(self):
        """ فیلد null/blank است؛ اعتبارسنجی مدل نباید مقدار خالی را رد کند. """
        settings_obj = SiteSettings.load()
        settings_obj.full_clean()   # نباید ValidationError بدهد

    def test_can_save_and_reload_a_datetime_value(self):
        moment = timezone.now().replace(microsecond=0)
        settings_obj = SiteSettings.load()
        settings_obj.loyalty_activated_at = moment
        settings_obj.save()

        reloaded = SiteSettings.objects.get(pk=1)
        self.assertEqual(reloaded.loyalty_activated_at, moment)

        cached = SiteSettings.cached()
        self.assertEqual(cached.loyalty_activated_at, moment)

    def test_can_be_cleared_back_to_null(self):
        settings_obj = SiteSettings.load()
        settings_obj.loyalty_activated_at = timezone.now()
        settings_obj.save()

        settings_obj.loyalty_activated_at = None
        settings_obj.save()
        self.assertIsNone(SiteSettings.objects.get(pk=1).loyalty_activated_at)

    def test_other_loyalty_settings_remain_unchanged(self):
        """ افزودن این فیلد نباید هیچ مقدار/رفتار دیگری از بلوک وفاداری موجود را عوض کند. """
        settings_obj = SiteSettings.load()
        self.assertEqual(settings_obj.loyalty_mode, SiteSettings.LOYALTY_MODE_ORDER_COUNT)
        self.assertEqual(settings_obj.loyalty_points_per_order, 100)
        self.assertEqual(settings_obj.loyalty_amount_step, 100000)
        self.assertEqual(settings_obj.loyalty_threshold_bronze, 300)
        self.assertEqual(settings_obj.loyalty_threshold_silver, 700)
        self.assertEqual(settings_obj.loyalty_threshold_gold, 1500)
        self.assertEqual(settings_obj.loyalty_threshold_diamond, 3000)

        settings_obj.loyalty_activated_at = timezone.now()
        settings_obj.save()
        settings_obj.full_clean()   # قیدهای صعودی‌بودن آستانه‌ها همچنان برقرارند

    def test_setting_the_activation_moment_creates_no_ledger_side_effects(self):
        """
        محور «بدون بک‌فیل»: تنظیم این فیلد (حتی با کاربران/سفارش‌های موجود در دیتابیس) به‌خودی‌خود
        هیچ LoyaltyAccount/LoyaltyTransaction ای نمی‌سازد - چون هنوز هیچ Receiver/Signal ای در
        فاز ۲A نوشته نشده که به این مقدار واکنش نشان دهد.
        """
        user = CustomUser.objects.create_user(phone_number='09120790001')
        order = Order.objects.create(
            user=user, first_name='کاربر', last_name='تست', phone=user.phone_number,
            address='تهران', payment_method='cash', total_price=100000,
        )
        Transaction.objects.create(
            user=user, order=order, amount=order.total_price, authority='TEST-2A-BACKFILL', status='success',
        )

        settings_obj = SiteSettings.load()
        settings_obj.loyalty_activated_at = timezone.now() - timezone.timedelta(days=365)   # حتی در گذشته‌ی دور
        settings_obj.save()

        self.assertFalse(LoyaltyAccount.objects.filter(user=user).exists())
        self.assertFalse(LoyaltyTransaction.objects.exists())


class LoyaltyActivationAdminTests(TestCase):
    def setUp(self):
        SiteSettings.load().save()
        self.admin = CustomUser.objects.create_superuser(phone_number='09120790099')
        self.client.force_login(self.admin)
        self.addCleanup(self._reset)

    def _reset(self):
        obj = SiteSettings.load()
        obj.loyalty_activated_at = None
        obj.save()

    def test_field_is_registered_on_the_loyalty_fieldset(self):
        model_admin = admin_site._registry[SiteSettings]
        loyalty_fieldset = next(f for name, f in model_admin.fieldsets if name == 'امتیاز و سطح مشتریان')
        self.assertIn('loyalty_activated_at', loyalty_fieldset['fields'])

    def test_admin_change_page_shows_the_field(self):
        response = self.client.get(reverse('admin:products_sitesettings_change', args=[1]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="loyalty_activated_at')

    def _post_all(self, **overrides):
        from django.db.models.fields.files import FieldFile
        from django.forms.models import model_to_dict
        data = model_to_dict(SiteSettings.load())
        data.update(overrides)
        data = {
            k: v for k, v in data.items()
            if v is not None and v is not False and not (isinstance(v, FieldFile) and not v)
        }
        return self.client.post(reverse('admin:products_sitesettings_change', args=[1]), data)

    def test_saving_a_value_through_the_admin_form(self):
        """ ویجت پیش‌فرض ادمین برای DateTimeField دو فیلد جدا (تاریخ/ساعت) پست می‌کند، نه یک رشته‌ی واحد. """
        response = self._post_all(loyalty_activated_at_0='2026-01-01', loyalty_activated_at_1='00:00:00')
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(SiteSettings.load().loyalty_activated_at)

    def test_leaving_the_field_blank_keeps_it_null(self):
        response = self._post_all()   # loyalty_activated_at از قبل None است؛ توسط فیلتر None حذف می‌شود
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(SiteSettings.load().loyalty_activated_at)
