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
        data = {k: v for k, v in data.items() if v is not None and v is not False}      # چک‌باکس خاموش = ارسال‌نشدن (صفر عددی ارسال می‌شود)
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


class SiteSettingsGuestPricingTests(TestCase):
    """ فاز ۱ قیمت مهمان: فیلدها، پیش‌فرض‌ها، اعتبارسنجی (clean و قیدهای دیتابیس) و ادمین """

    FIELDS = ('guest_pricing_mode', 'guest_price_level', 'guest_adjustment_type', 'guest_adjustment_value',
              'guest_price_rounding_step', 'guest_price_hidden_message')

    def setUp(self):
        from products.models import SiteSettings
        self.SiteSettings = SiteSettings
        SiteSettings.load().save()
        self.admin = CustomUser.objects.create_superuser(phone_number='09120005011')
        self.client.force_login(self.admin)

    def settings_with(self, **fields):
        obj = self.SiteSettings.load()
        for name, value in fields.items():
            setattr(obj, name, value)
        return obj

    def assertInvalid(self, field, **fields):
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError) as caught:
            self.settings_with(**fields).full_clean()
        self.assertIn(field, caught.exception.message_dict, fields)

    def assertValid(self, **fields):
        self.settings_with(**fields).full_clean()

    # --- پیش‌فرض‌ها و گزینه‌ها ---
    def test_defaults_keep_the_previous_behaviour(self):
        from products.pricing import GUEST_HIDDEN_MESSAGE_DEFAULT
        row = self.SiteSettings.load()
        self.assertEqual(row.guest_pricing_mode, 'price_level')            # مهمان = قیمت یک سطح...
        self.assertEqual(row.guest_price_level, 1)                         # ...و آن سطح ۱ (چکی) است؛ مثل قبل
        self.assertEqual((row.guest_adjustment_type, row.guest_adjustment_value, row.guest_price_rounding_step),
                         ('percent', 0, 1))
        self.assertEqual(row.guest_price_hidden_message, GUEST_HIDDEN_MESSAGE_DEFAULT)
        self.assertNotRegex(row.guest_price_hidden_message, '[يك]')       # ی/ک فارسی، نه عربی
        row.full_clean()

    def test_choices_follow_the_project_price_level_mapping(self):
        field = self.SiteSettings._meta.get_field
        levels = dict(field('guest_price_level').choices)
        self.assertEqual(sorted(levels), list(range(1, 11)))
        self.assertIn('چکی', levels[1])
        self.assertIn('نقدی', levels[2])
        for level in range(3, 11):
            self.assertIn('ویژه', levels[level])
        self.assertEqual([k for k, _ in field('guest_pricing_mode').choices],
                         ['hide_price', 'price_level', 'calculated_price'])
        self.assertEqual([k for k, _ in field('guest_adjustment_type').choices], ['percent', 'fixed'])
        self.assertEqual([k for k, _ in field('guest_price_rounding_step').choices], [1, 100, 1000])

    # --- اعتبارسنجی clean() ---
    def test_percent_adjustment_must_stay_between_minus_90_and_plus_500(self):
        base = dict(guest_pricing_mode='calculated_price', guest_adjustment_type='percent')
        for good in ('-90', '-0.5', '12.5', '15', '500'):
            with self.subTest(good=good):
                self.assertValid(guest_adjustment_value=Decimal(good), **base)
        for bad in ('-90.01', '-91', '-100', '500.01', '501'):
            with self.subTest(bad=bad):
                self.assertInvalid('guest_adjustment_value', guest_adjustment_value=Decimal(bad), **base)

    def test_fixed_adjustment_must_be_a_whole_number_either_sign(self):
        base = dict(guest_pricing_mode='calculated_price', guest_adjustment_type='fixed')
        for good in ('50000', '-20000', '1', '-1', '5000000'):
            with self.subTest(good=good):
                self.assertValid(guest_adjustment_value=Decimal(good), **base)
        for bad in ('1500.5', '-0.01', '99999.99'):
            with self.subTest(bad=bad):
                self.assertInvalid('guest_adjustment_value', guest_adjustment_value=Decimal(bad), **base)

    def test_zero_adjustment_is_refused_only_in_calculated_mode(self):
        for kind in ('percent', 'fixed'):
            with self.subTest(kind=kind):
                self.assertInvalid('guest_adjustment_value', guest_pricing_mode='calculated_price',
                                   guest_adjustment_type=kind, guest_adjustment_value=0)
                for mode in ('hide_price', 'price_level'):
                    self.assertValid(guest_pricing_mode=mode, guest_adjustment_type=kind, guest_adjustment_value=0)

    def test_stale_out_of_range_value_is_still_caught_when_mode_is_not_calculated(self):
        # مقدار ذخیره‌شده‌ی خارج از بازه، حتی با حالت غیرفرمولی، وارد دیتابیس نمی‌شود (قید و clean یکی هستند)
        self.assertInvalid('guest_adjustment_value', guest_pricing_mode='price_level', guest_adjustment_type='percent',
                           guest_adjustment_value=900)

    def test_hidden_message_cannot_be_blank_or_whitespace(self):
        for text in ('', '   ', '\t \n'):
            with self.subTest(text=repr(text)):
                self.assertInvalid('guest_price_hidden_message', guest_price_hidden_message=text)
        self.assertValid(guest_price_hidden_message='برای دیدن قیمت‌ها وارد شوید')

    def test_level_and_rounding_step_must_be_allowed_choices(self):
        for level in (0, 11, -1):
            with self.subTest(level=level):
                self.assertInvalid('guest_price_level', guest_price_level=level)
        for level in range(1, 11):
            self.assertValid(guest_price_level=level)
        for step in (0, 5, 50, 10000):
            with self.subTest(step=step):
                self.assertInvalid('guest_price_rounding_step', guest_price_rounding_step=step)
        for step in (1, 100, 1000):
            self.assertValid(guest_price_rounding_step=step)

    # --- قیدهای دیتابیس (نوشتن مستقیم بدون full_clean) ---
    def test_database_constraints_refuse_direct_invalid_writes(self):
        from django.db import IntegrityError, transaction
        for change in ({'guest_price_level': 0}, {'guest_price_level': 11}, {'guest_price_rounding_step': 50},
                       {'guest_adjustment_type': 'percent', 'guest_adjustment_value': 600},
                       {'guest_adjustment_type': 'percent', 'guest_adjustment_value': -95}):
            with self.subTest(change=change), self.assertRaises(IntegrityError), transaction.atomic():
                self.SiteSettings.objects.filter(pk=1).update(**change)
        # مبلغ ثابت به قید درصدی گیر نمی‌کند
        self.SiteSettings.objects.filter(pk=1).update(guest_adjustment_type='fixed', guest_adjustment_value=-250000)
        self.assertEqual(int(self.SiteSettings.load().guest_adjustment_value), -250000)

    # --- ادمین ---
    def _form(self, **overrides):
        from django.forms.models import model_to_dict
        data = model_to_dict(self.SiteSettings.load())
        data.update(overrides)
        return {k: v for k, v in data.items() if v is not None and v is not False}

    def _post(self, **overrides):
        return self.client.post(reverse('admin:products_sitesettings_change', args=[1]), self._form(**overrides))

    def test_admin_shows_the_section_all_fields_and_the_toggle_script(self):
        response = self.client.get(reverse('admin:products_sitesettings_change', args=[1]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'قیمت برای کاربران مهمان')
        for name in self.FIELDS:
            with self.subTest(field=name):
                self.assertContains(response, f'name="{name}"')
        self.assertContains(response, 'products/admin/guest_pricing_toggle.js')

    def test_admin_can_switch_modes_and_the_cache_is_fresh_immediately(self):
        response = self._post(guest_pricing_mode='calculated_price', guest_price_level=2, guest_adjustment_type='percent',
                              guest_adjustment_value='15', guest_price_rounding_step=100,
                              guest_price_hidden_message='برای مشاهده قیمت وارد شوید')
        self.assertEqual(response.status_code, 302)
        for stored in (self.SiteSettings.load(), self.SiteSettings.cached()):
            self.assertEqual(stored.guest_pricing_mode, 'calculated_price')
            self.assertEqual(stored.guest_price_level, 2)
            self.assertEqual(int(stored.guest_adjustment_value), 15)
            self.assertEqual(stored.guest_price_rounding_step, 100)
            self.assertEqual(stored.guest_price_hidden_message, 'برای مشاهده قیمت وارد شوید')

        response = self._post(guest_pricing_mode='hide_price', guest_price_hidden_message='ابتدا وارد شوید')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.SiteSettings.cached().guest_pricing_mode, 'hide_price')
        self.assertEqual(self.SiteSettings.cached().guest_price_hidden_message, 'ابتدا وارد شوید')

    def test_admin_rejects_invalid_values_with_a_message_and_stores_nothing(self):
        cases = (
            (dict(guest_pricing_mode='calculated_price', guest_adjustment_type='percent', guest_adjustment_value='900'),
             'بین'),
            (dict(guest_pricing_mode='calculated_price', guest_adjustment_type='fixed', guest_adjustment_value='1500.5'),
             'عدد صحیح'),
            (dict(guest_pricing_mode='calculated_price', guest_adjustment_type='percent', guest_adjustment_value='0'),
             'نباید صفر باشد'),
            # رشته‌ی فقط‌فاصله را خودِ CharField (strip=True) قبل از رسیدن به clean() به '' تبدیل می‌کند؛
            # خطای همان الزام استاندارد فیلد دیده می‌شود، نه پیام سفارشی clean() (که فقط مسیرهای غیرفرمی/برنامه‌ای را می‌پوشاند)
            (dict(guest_price_hidden_message='   '), 'این فیلد لازم است'),
            (dict(guest_price_level='11'), 'errorlist'),
        )
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                response = self._post(**overrides)
                self.assertEqual(response.status_code, 200)                # فرم با خطا برگشت
                self.assertContains(response, expected)
                stored = self.SiteSettings.load()
                self.assertEqual((stored.guest_pricing_mode, stored.guest_price_level), ('price_level', 1))

    def test_staff_without_permission_cannot_change_it(self):
        staff = CustomUser.objects.create_user(phone_number='09120005012', is_staff=True)
        self.client.force_login(staff)
        response = self.client.post(reverse('admin:products_sitesettings_change', args=[1]),
                                    self._form(guest_pricing_mode='hide_price'))
        self.assertIn(response.status_code, (302, 403))
        self.assertEqual(self.SiteSettings.load().guest_pricing_mode, 'price_level')


class SiteSettingsGuestPricingMigrationTests(TransactionTestCase):
    """ مایگریشن 0022 روی ردیف موجود: پیش‌فرض‌ها، متن فارسی سالم (نه ي/ك عربی) و کش تازه """

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

    def test_existing_row_keeps_its_data_and_gets_safe_defaults(self):
        from django.core.cache import cache
        from products.pricing import GUEST_HIDDEN_MESSAGE_DEFAULT
        old_apps = self._migrate(('products', '0021_delete_discount'))
        Old = old_apps.get_model('products', 'SiteSettings')
        Old.objects.all().delete()
        Old.objects.create(pk=1, phone='02537700000', copyright_text='متن اختصاصی کپی‌رایت')
        cache.set('storefront:site_settings', 'کش قدیمی', 60)

        new_apps = self._migrate(('products', '0022_sitesettings_guest_pricing'))
        row = new_apps.get_model('products', 'SiteSettings').objects.get(pk=1)

        self.assertEqual(row.guest_price_hidden_message, GUEST_HIDDEN_MESSAGE_DEFAULT)
        self.assertNotRegex(row.guest_price_hidden_message, '[يك]')
        self.assertEqual((row.guest_pricing_mode, row.guest_price_level, row.guest_adjustment_type,
                          int(row.guest_adjustment_value), row.guest_price_rounding_step),
                         ('price_level', 1, 'percent', 0, 1))
        self.assertEqual((row.phone, row.copyright_text), ('02537700000', 'متن اختصاصی کپی‌رایت'))
        self.assertIsNone(cache.get('storefront:site_settings'))          # نسخه‌ی کش‌شده‌ی بدون فیلدهای تازه پاک شد

    def test_reverse_migration_drops_the_columns_and_constraints(self):
        from django.db import connection
        self._migrate(('products', '0022_sitesettings_guest_pricing'))
        self._migrate(('products', '0021_delete_discount'))
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM information_schema.columns WHERE table_name = 'products_sitesettings' "
                           "AND column_name LIKE 'guest[_]%'")
            self.assertEqual(cursor.fetchone()[0], 0)
