"""تست قیمت‌گذاری — تنها منبع حقیقت قیمت در کل پروژه."""

from decimal import Decimal

from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from django.urls import reverse

from accounts.models import CustomUser
from products.models import Category, Product, StockAlert
from promotions.testing import PromotionTestMixin, make_promotion
from products.pricing import (
    CASH, CHECK, VIP, base_price, default_payment_method, final_price, resolve_payment_method,
)


class PricingTests(PromotionTestMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.category = Category.objects.create(name='تست', slug='test-cat')
        cls.product = Product.objects.create(
            name='کالای تست', slug='test-product', erp_code='ERP-TEST-1',
            category=cls.category, price=100000, price2=90000, price3=80000, stock=10,
        )
        cls.level1 = CustomUser.objects.create_user(phone_number='09120000001', price_level=1)
        cls.level2 = CustomUser.objects.create_user(phone_number='09120000002', price_level=2)
        cls.level3 = CustomUser.objects.create_user(phone_number='09120000003', price_level=3)

    # --- نگاشت روش پرداخت به ستون قیمت ---

    def test_payment_method_maps_to_correct_price_column(self):
        self.assertEqual(base_price(self.product, self.level1, CHECK), Decimal('100000'))
        self.assertEqual(base_price(self.product, self.level1, CASH), Decimal('90000'))
        self.assertEqual(base_price(self.product, self.level3, VIP), Decimal('80000'))

    def test_default_method_follows_user_price_level(self):
        self.assertEqual(default_payment_method(self.level1), CHECK)
        self.assertEqual(default_payment_method(self.level2), CASH)
        self.assertEqual(default_payment_method(self.level3), VIP)

    def test_zero_tier_price_falls_back_to_price_one(self):
        """ اگر قیمت آن سطح در هلو پر نشده باشد نباید فاکتور با مبلغ صفر ثبت شود """
        self.product.price2 = 0
        self.assertEqual(base_price(self.product, self.level1, CASH), Decimal('100000'))

    # --- امنیت انتخاب روش پرداخت ---

    def test_vip_user_is_locked_to_vip_even_if_form_says_otherwise(self):
        self.assertEqual(resolve_payment_method(self.level3, CHECK), VIP)

    def test_price_level_above_three_is_also_vip(self):
        """ سطح ۴ تا ۱۰ هم ویژه‌اند؛ قبلاً سبد priceN می‌داد ولی فاکتور price1 ثبت می‌کرد """
        user = CustomUser.objects.create_user(phone_number='09120000005', price_level=5)
        self.assertEqual(resolve_payment_method(user, CHECK), VIP)

    def test_invalid_method_falls_back_to_user_default(self):
        self.assertEqual(resolve_payment_method(self.level1, 'FREE'), CHECK)
        self.assertEqual(resolve_payment_method(self.level2, ''), CASH)

    # --- تخفیف ---

    def _add_discount(self, percent=25, active=True, expired=False):
        return make_promotion(self.product, percent=percent, active=active, expired=expired)

    def test_active_discount_is_applied(self):
        self._add_discount(25)
        self.assertEqual(final_price(self.product, self.level1, CHECK), Decimal('75000'))

    def test_expired_discount_is_ignored(self):
        self._add_discount(25, expired=True)
        self.assertEqual(final_price(self.product, self.level1, CHECK), Decimal('100000'))

    def test_inactive_discount_is_ignored(self):
        self._add_discount(25, active=False)
        self.assertEqual(final_price(self.product, self.level1, CHECK), Decimal('100000'))

    def test_best_discount_wins(self):
        self._add_discount(10)
        self._add_discount(40)
        self.assertEqual(final_price(self.product, self.level1, CHECK), Decimal('60000'))

    def test_discount_applies_on_top_of_payment_method_price(self):
        self._add_discount(50)
        self.assertEqual(final_price(self.product, self.level1, CASH), Decimal('45000'))

    def test_price_is_rounded_to_whole_toman(self):
        """ عدد نمایش‌داده‌شده و عدد ذخیره‌شده در OrderItem باید دقیقاً یکی باشند """
        self.product.price = 33333
        self._add_discount(33)
        self.assertEqual(final_price(self.product, self.level1, CHECK), Decimal('22333'))

    # --- هماهنگی کارت / سبد / فاکتور ---

    def test_card_cart_and_invoice_agree(self):
        from cart.models import Cart, CartItem
        self._add_discount(20)

        card = self.product.get_discounted_price(self.level1)
        invoice = final_price(self.product, self.level1, resolve_payment_method(self.level1, CHECK))

        cart = Cart.objects.create(user=self.level1)
        item = CartItem.objects.create(cart=cart, product=self.product, quantity=3)

        self.assertEqual(card, invoice)
        self.assertEqual(item.get_cost(), card * 3)

    def test_anonymous_user_gets_price_one(self):
        self.assertEqual(base_price(self.product, None), Decimal('100000'))


class ProductDetailQueryCountTests(TestCase):
    """
    اثبات عملی رفع دو کوئری اضافه‌ی صفحه‌ی محصول:
    - context['features'] با select_related('feature').all() تازه، prefetch بالای
      get_queryset را دور می‌زد و یک کوئری اضافه می‌زد.
    - category.parent بدون select_related('category__parent') یک کوئری جدا می‌زد.
    """

    @classmethod
    def setUpTestData(cls):
        parent_category = Category.objects.create(name='دسته والد', slug='detail-parent-cat')
        cls.category = Category.objects.create(name='دسته فرزند', slug='detail-child-cat', parent=parent_category)
        cls.product = Product.objects.create(
            name='کالای تست جزئیات', slug='detail-query-test', erp_code='ERP-DETAIL-1',
            category=cls.category, price=100000, stock=10,
        )
        from products.models import Feature, ProductColor, ProductFeatureValue
        ProductColor.objects.create(product=cls.product, name='قرمز')
        feature = Feature.objects.create(name='جنس')
        ProductFeatureValue.objects.create(product=cls.product, feature=feature, value='چوب')

    def test_category_parent_uses_select_related_not_a_new_query(self):
        from products.views import ProductDetailView
        with self.assertNumQueries(5):  # اصلی + colors + gallery_images + features + feature
            product = ProductDetailView().get_queryset().get(pk=self.product.pk)
        with self.assertNumQueries(0):
            self.assertEqual(product.category.parent.slug, 'detail-parent-cat')

    def test_features_context_reuses_prefetch_cache(self):
        from products.views import ProductDetailView
        product = ProductDetailView().get_queryset().get(pk=self.product.pk)
        with self.assertNumQueries(0):
            features = list(product.features.all())
        self.assertEqual(len(features), 1)


class ProductListDistinctTests(TestCase):
    """
    distinct() روی فهرست فروشگاه فقط باید وقتی اعمال شود که فیلتر رنگ/مشخصات فنی (که با JOIN
    روی یک رابطه‌ی چندتایی می‌آیند و می‌توانند محصول را تکراری کنند) فعال باشد؛ برای بازدید
    ساده‌ی صفحه‌بندی‌شده (بدون این فیلترها) اضافه‌کردنش فقط COUNT/SELECT را روی کل کاتالوگ
    بی‌جهت گران‌تر می‌کرد.
    """

    @classmethod
    def setUpTestData(cls):
        from products.models import ProductColor

        category = Category.objects.create(name='دسته تست فهرست', slug='list-distinct-cat')
        cls.product = Product.objects.create(
            name='کالای چندرنگ', slug='list-distinct-product', erp_code='ERP-LIST-DISTINCT-1',
            category=category, price=100000, stock=5,
        )
        # دو رنگ با نام یکسان تا فیلتر رنگ روی این محصول دو ردیف JOIN‌شده بدهد
        ProductColor.objects.create(product=cls.product, name='قرمز')
        ProductColor.objects.create(product=cls.product, name='قرمز')

    def test_unfiltered_listing_does_not_use_distinct(self):
        response = self.client.get('/shop/')
        self.assertNotIn('DISTINCT', str(response.context['page_obj'].paginator.object_list.query))

    def test_color_filter_uses_distinct_and_avoids_duplicates(self):
        response = self.client.get('/shop/', {'color': 'قرمز'})
        queryset = response.context['page_obj'].paginator.object_list
        self.assertIn('DISTINCT', str(queryset.query))
        # بدون distinct، همین محصول به‌خاطر دو ردیف JOIN رنگ، دوبار در paginator.count می‌آمد
        self.assertEqual(response.context['page_obj'].paginator.count, 1)


class StockAlertViewTests(TestCase):
    """ ثبت/لغو درخواست «اطلاع بده وقتی موجود شد» — products/views.py::StockAlertView """

    def setUp(self):
        self.user = CustomUser.objects.create_user(phone_number='09120000060')
        category = Category.objects.create(name='تست', slug='stock-alert-test-cat')
        self.product = Product.objects.create(
            name='کالای ناموجود', slug='stock-alert-test-product', erp_code='ERP-STOCK-ALERT-1',
            category=category, price=100000, stock=0,
        )
        self.client.force_login(self.user)

    def _post(self, **data):
        return self.client.post(reverse('products:stock_alert', args=[self.product.id]), data)

    def test_sms_subscription_creates_pending_alert(self):
        self._post(channel='sms')
        alert = StockAlert.objects.get(product=self.product, user=self.user)
        self.assertEqual(alert.channel, StockAlert.CHANNEL_SMS)
        self.assertEqual(alert.status, StockAlert.STATUS_PENDING)

    def test_email_subscription_without_any_email_shows_error(self):
        response = self._post(channel='email')
        self.assertContains(response, 'ایمیل')
        self.assertFalse(StockAlert.objects.filter(product=self.product, user=self.user).exists())

    def test_email_subscription_captures_and_saves_email_to_profile(self):
        """ طبق خواسته‌ی کارفرما: اگر کاربر ایمیل نداشت، همین‌جا گرفته و در پروفایلش هم ذخیره شود """
        self._post(channel='email', email='test@example.com')
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'test@example.com')
        alert = StockAlert.objects.get(product=self.product, user=self.user)
        self.assertEqual(alert.channel, StockAlert.CHANNEL_EMAIL)
        self.assertEqual(alert.email, '')  # همان ایمیل پروفایل استفاده می‌شود، نیازی به کپی جدا نیست

    def test_existing_profile_email_can_be_overridden_for_this_alert_only(self):
        """
        کاربری که از قبل در پروفایل ایمیل دارد، می‌تواند اینجا ایمیل دیگری فقط برای همین
        اطلاع‌رسانی بدهد؛ ایمیل پروفایلش نباید عوض شود.
        """
        self.user.email = 'profile@example.com'
        self.user.save(update_fields=['email'])

        self._post(channel='email', email='different@example.com')

        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'profile@example.com')
        alert = StockAlert.objects.get(product=self.product, user=self.user)
        self.assertEqual(alert.email, 'different@example.com')

    def test_invalid_email_shows_error_and_does_not_save_anything(self):
        self._post(channel='email', email='not-an-email')
        self.user.refresh_from_db()
        self.assertFalse(self.user.email)
        self.assertFalse(StockAlert.objects.filter(product=self.product, user=self.user).exists())

    def test_existing_profile_email_is_reused_without_resubmitting(self):
        self.user.email = 'already@example.com'
        self.user.save(update_fields=['email'])
        self._post(channel='email')
        alert = StockAlert.objects.get(product=self.product, user=self.user)
        self.assertEqual(alert.channel, StockAlert.CHANNEL_EMAIL)
        self.assertEqual(alert.email, '')  # override نشده؛ همان ایمیل پروفایل استفاده می‌شود

    def test_cancel_deletes_alert(self):
        StockAlert.objects.create(product=self.product, user=self.user, channel=StockAlert.CHANNEL_SMS)
        self._post(action='cancel')
        self.assertFalse(StockAlert.objects.filter(product=self.product, user=self.user).exists())

    def test_resubscribing_resets_notified_alert_to_pending(self):
        """ اگر قبلاً یک‌بار اطلاع داده شده و کالا دوباره ناموجود/موجود شد، دوباره درخواست می‌تواند فعال شود """
        StockAlert.objects.create(
            product=self.product, user=self.user, channel=StockAlert.CHANNEL_SMS,
            status=StockAlert.STATUS_NOTIFIED, notified_at=timezone.now(),
        )
        self._post(channel='sms')
        alert = StockAlert.objects.get(product=self.product, user=self.user)
        self.assertEqual(alert.status, StockAlert.STATUS_PENDING)
        self.assertIsNone(alert.notified_at)

    def test_subscribing_when_already_in_stock_is_a_no_op(self):
        self.product.stock = 5
        self.product.save(update_fields=['stock'])
        self._post(channel='sms')
        self.assertFalse(StockAlert.objects.filter(product=self.product, user=self.user).exists())

    def test_anonymous_user_is_redirected_to_login(self):
        self.client.logout()
        response = self._post(channel='sms')
        self.assertEqual(response.status_code, 302)


class SiteSettingsShippingPolicyTests(TestCase):
    """ فیلدهای کنترلیِ سیاست هزینه‌ی حمل در تنظیمات سایت (فاز ب) """

    POLICY_FIELDS = ('courier_free_for_free_shipping_cart', 'postage_collect_enabled',
                     'postage_collect_label', 'postage_disabled_message')

    def setUp(self):
        from products.models import SiteSettings
        self.SiteSettings = SiteSettings
        SiteSettings.load().save()                   # ردیف تنظیمات با مقادیر پیش‌فرض
        self.admin = CustomUser.objects.create_superuser(phone_number='09120005001')
        self.client.force_login(self.admin)

    def test_defaults_match_previous_behaviour(self):
        settings_obj = self.SiteSettings.load()
        self.assertTrue(settings_obj.courier_free_for_free_shipping_cart)      # همان رفتار قبلیِ «ارسال رایگان»
        self.assertTrue(settings_obj.postage_collect_enabled)
        self.assertEqual(settings_obj.postage_collect_label, 'پس‌کرایه (پرداخت هزینه درب منزل)')
        self.assertEqual(settings_obj.postage_disabled_message, 'امکان ارسال به این شهر فعلاً وجود ندارد.')

    def test_admin_shows_the_policy_section(self):
        response = self.client.get(reverse('admin:products_sitesettings_change', args=[1]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'سیاست هزینه‌ی حمل')
        for field in self.POLICY_FIELDS:
            with self.subTest(field=field):
                self.assertContains(response, f'name="{field}"')

    def _post_all(self, **overrides):
        """ فرم ادمین تنظیمات سایت با همه‌ی فیلدهای فعلی + تغییرات """
        from django.forms.models import model_to_dict
        data = model_to_dict(self.SiteSettings.load())
        data.update(overrides)
        data = {k: v for k, v in data.items() if v not in (None, False)}      # چک‌باکس خاموش = ارسال‌نشدن
        return self.client.post(reverse('admin:products_sitesettings_change', args=[1]), data)

    def test_admin_can_change_the_policy_and_it_takes_effect_immediately(self):
        response = self._post_all(postage_collect_label='ارسال با پست؛ کرایه را تحویل‌گیرنده می‌پردازد',
                                  postage_disabled_message='فعلاً فقط داخل قم ارسال داریم.',
                                  courier_free_for_free_shipping_cart=False, postage_collect_enabled=False)
        self.assertEqual(response.status_code, 302)
        for stored in (self.SiteSettings.load(), self.SiteSettings.cached()):      # کش هم تازه شده
            self.assertEqual(stored.postage_collect_label, 'ارسال با پست؛ کرایه را تحویل‌گیرنده می‌پردازد')
            self.assertEqual(stored.postage_disabled_message, 'فعلاً فقط داخل قم ارسال داریم.')
            self.assertFalse(stored.courier_free_for_free_shipping_cart)           # چک‌باکس ارسال‌نشده = خاموش
            self.assertFalse(stored.postage_collect_enabled)

    def test_label_and_message_cannot_be_blank(self):
        response = self._post_all(postage_collect_label='', postage_disabled_message='')
        self.assertEqual(response.status_code, 200)                                 # فرم با خطا برگشت
        self.assertTrue(self.SiteSettings.load().postage_collect_label)


class SiteSettingsPolicyMigrationTests(TransactionTestCase):
    """ AddField با default فارسی روی ردیفِ موجود، روی SQL Server حروف «ی/ک» را به «ي/ك» عربی تبدیل می‌کرد؛
    مایگریشن 0019 مقدار را با ORM دوباره می‌نویسد. """

    serialized_rollback = True

    def tearDown(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())

    def _migrate(self, target):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        MigrationExecutor(connection).migrate([target])
        return MigrationExecutor(connection).loader.project_state([target]).apps

    def test_existing_row_gets_proper_persian_defaults(self):
        old_apps = self._migrate(('products', '0018_alter_stockalert_channel'))
        old_apps.get_model('products', 'SiteSettings').objects.all().delete()
        old_apps.get_model('products', 'SiteSettings').objects.create(pk=1)

        new_apps = self._migrate(('products', '0019_sitesettings_shipping_policy'))
        row = new_apps.get_model('products', 'SiteSettings').objects.get(pk=1)

        self.assertEqual(row.postage_collect_label, 'پس‌کرایه (پرداخت هزینه درب منزل)')
        self.assertEqual(row.postage_disabled_message, 'امکان ارسال به این شهر فعلاً وجود ندارد.')
        for text in (row.postage_collect_label, row.postage_disabled_message):
            self.assertNotRegex(text, '[يك]')                    # حروف عربی نباید نشسته باشند
        self.assertTrue(row.courier_free_for_free_shipping_cart)
        self.assertTrue(row.postage_collect_enabled)


class SiteSettingsShippingCostRemovedTests(TestCase):
    """ هزینه‌ی ارسال ثابتِ تنظیمات سایت حذف شده؛ کرایه از ناحیه/پس‌کرایه می‌آید """

    def test_field_is_gone_from_the_model_and_the_database(self):
        from django.core.exceptions import FieldDoesNotExist
        from django.db import connection
        from products.models import SiteSettings
        with self.assertRaises(FieldDoesNotExist):
            SiteSettings._meta.get_field('shipping_cost')
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM information_schema.columns "
                           "WHERE table_name = 'products_sitesettings' AND column_name = 'shipping_cost'")
            self.assertEqual(cursor.fetchone()[0], 0)
        SiteSettings.load()                                    # ردیف تنظیمات بدون این ستون هم ساخته/خوانده می‌شود

    def test_admin_form_no_longer_offers_it_but_keeps_the_erp_code(self):
        from products.models import SiteSettings
        SiteSettings.load()
        admin_user = CustomUser.objects.create_superuser(phone_number='09120005002')
        self.client.force_login(admin_user)
        response = self.client.get(reverse('admin:products_sitesettings_change', args=[1]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="shipping_cost"')
        self.assertContains(response, 'name="shipping_erp_code"')
