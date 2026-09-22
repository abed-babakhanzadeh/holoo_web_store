"""تست قیمت‌گذاری — تنها منبع حقیقت قیمت در کل پروژه."""

from decimal import Decimal

from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from django.urls import reverse

from accounts.models import CustomUser
from products.models import Category, Product, SiteSettings, StockAlert
from promotions.models import DiscountPolicy, Promotion
from promotions.testing import PromotionTestMixin, make_promotion, reset_promotions_cache
from products.pricing import (
    CASH, CHECK, GUEST_HIDDEN_MESSAGE_DEFAULT, VIP, base_price, default_payment_method, final_price, price_breakdown,
    resolve_payment_method,
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


class GuestPricingTestBase(TestCase):
    """ پایه‌ی مشترک تست‌های هسته‌ی قیمت مهمان: یک کالا با همه‌ی سطوح و کمکی برای تغییر SiteSettings """

    @classmethod
    def setUpTestData(cls):
        cls.category = Category.objects.create(name='تست مهمان', slug='guest-test-cat')
        cls.product = Product.objects.create(
            name='کالای تست مهمان', slug='guest-test-product', erp_code='ERP-GUEST-1',
            category=cls.category, price=100000, price2=90000, price3=80000, stock=10,
        )

    def setUp(self):
        super().setUp()
        from products.models import SiteSettings
        self.SiteSettings = SiteSettings
        self.set_guest(mode='price_level', level=1, adj_type='percent', adj_value=0, step=1)
        self.addCleanup(lambda: self.set_guest(mode='price_level', level=1, adj_type='percent', adj_value=0, step=1))

    def set_guest(self, *, mode=None, level=None, adj_type=None, adj_value=None, step=None, message=None):
        """ تغییر SiteSettings.guest_* با save() واقعی (نه update())، تا سیگنال کش/memo را باطل کند """
        obj = self.SiteSettings.load()
        if mode is not None:
            obj.guest_pricing_mode = mode
        if level is not None:
            obj.guest_price_level = level
        if adj_type is not None:
            obj.guest_adjustment_type = adj_type
        if adj_value is not None:
            obj.guest_adjustment_value = Decimal(str(adj_value))
        if step is not None:
            obj.guest_price_rounding_step = step
        if message is not None:
            obj.guest_price_hidden_message = message
        obj.full_clean()
        obj.save()
        return obj


class GuestPricingLevelModeTests(GuestPricingTestBase):
    """ حالت ب: نمایش یکی از قیمت‌های ده‌گانه؛ همان مسیر base_price/price_breakdown، بدون هیچ محاسبه‌ی جدا """

    def test_guest_sees_configured_level_price(self):
        for level, expected in ((1, '100000'), (2, '90000'), (3, '80000')):
            with self.subTest(level=level):
                self.set_guest(level=level)
                self.assertEqual(base_price(self.product, None), Decimal(expected))
                self.assertEqual(final_price(self.product, None), Decimal(expected))

    def test_zero_tier_price_falls_back_to_level_one_for_guest_too(self):
        """ همان fallback موجود برای کاربر واردشده؛ قبلاً Product.get_user_price این را فقط برای کاربر واردشده می‌داد """
        self.set_guest(level=4)                          # price4 پیش‌فرض صفر است
        self.assertEqual(base_price(self.product, None), Decimal('100000'))

    def test_default_config_reproduces_the_old_hardcoded_behaviour(self):
        """ پیش‌فرض SiteSettings (level=1) دقیقاً همان چیزی است که قبل از فاز ۱ برای مهمان ثابت بود """
        self.assertEqual(base_price(self.product, None), Decimal('100000'))

    def test_guest_price_breakdown_is_fully_visible_in_this_mode(self):
        breakdown = price_breakdown(self.product, None)
        self.assertTrue(breakdown.visible)
        self.assertEqual(breakdown.base, Decimal('100000'))


class GuestPricingCalculatedModeTests(GuestPricingTestBase):
    """ حالت ج: سطح پایه ← تعدیل ← گردکردن؛ ترتیب دقیق و حالت‌های مرزی """

    def test_percent_increase_and_decrease(self):
        self.set_guest(mode='calculated_price', level=1, adj_type='percent', adj_value=15)
        self.assertEqual(base_price(self.product, None), Decimal('115000'))       # +۱۵٪ روی ۱۰۰۰۰۰
        self.set_guest(adj_value=-20)
        self.assertEqual(base_price(self.product, None), Decimal('80000'))        # -۲۰٪ روی ۱۰۰۰۰۰

    def test_fixed_increase_and_decrease(self):
        self.set_guest(mode='calculated_price', level=2, adj_type='fixed', adj_value=25000)
        self.assertEqual(base_price(self.product, None), Decimal('115000'))       # ۹۰۰۰۰ (سطح ۲) + ۲۵۰۰۰
        self.set_guest(adj_value=-30000)
        self.assertEqual(base_price(self.product, None), Decimal('60000'))

    def test_base_level_selection_happens_before_adjustment(self):
        """ ترتیب صریح: ابتدا سطح پایه (سطح ۳ = ۸۰۰۰۰) انتخاب و بعد تعدیل روی همان اعمال می‌شود، نه روی سطح ۱ """
        self.set_guest(mode='calculated_price', level=3, adj_type='percent', adj_value=10)
        self.assertEqual(base_price(self.product, None), Decimal('88000'))        # ۸۰۰۰۰ × ۱٫۱

    def test_rounding_steps(self):
        self.set_guest(mode='calculated_price', level=1, adj_type='fixed', adj_value=1234)
        cases = {1: Decimal('101234'), 100: Decimal('101200'), 1000: Decimal('101000')}
        for step, expected in cases.items():
            with self.subTest(step=step):
                self.set_guest(step=step)
                self.assertEqual(base_price(self.product, None), expected)

    def test_rounding_never_produces_a_price_above_or_equal_negative(self):
        """ گرد کردن به سمت پایین می‌تواند یک نتیجه‌ی مثبتِ خیلی کوچک را هم به صفر برساند؛ باید همان‌جا fallback بخورد """
        self.set_guest(mode='calculated_price', level=1, adj_type='fixed', adj_value=-99500, step=1000)
        # ۱۰۰۰۰۰ - ۹۹۵۰۰ = ۵۰۰ ← گرد به نزدیک‌ترین هزار = ۱۰۰۰ (نه صفر، چون ۵۰۰/۱۰۰۰ رند بالا می‌رود)؛
        # این حالت را جدا هم پوشش می‌دهیم که مطمئن شویم عدد نهایی هیچ‌وقت غیرمنطقی/منفی نمی‌شود
        self.assertGreater(base_price(self.product, None), Decimal('0'))

    def test_fixed_adjustment_overshoot_falls_back_to_unadjusted_base_not_zero(self):
        """ مبلغ ثابتِ کاهشی بزرگ‌تر از قیمت پایه: نتیجه هرگز صفر/منفی نمی‌شود؛ به قیمت پایه‌ی بدون تعدیل برمی‌گردد """
        self.set_guest(mode='calculated_price', level=1, adj_type='fixed', adj_value=-500000)
        self.assertEqual(base_price(self.product, None), Decimal('100000'))       # نه صفر، نه منفی؛ خودِ قیمت پایه

    def test_percent_at_the_documented_boundaries(self):
        self.set_guest(mode='calculated_price', level=1, adj_type='percent', adj_value=-90)
        self.assertEqual(base_price(self.product, None), Decimal('10000'))
        self.set_guest(adj_value=500)
        self.assertEqual(base_price(self.product, None), Decimal('600000'))

    def test_discount_is_computed_on_top_of_the_adjusted_price_not_the_raw_level(self):
        """ زنجیره‌ی تک‌منبعی: سطح پایه ← تعدیل ← گردکردن ← تخفیف؛ درصد تخفیف روی قیمتِ *تعدیل‌شده* حساب می‌شود """
        self.set_guest(mode='calculated_price', level=1, adj_type='percent', adj_value=20)   # ۱۰۰۰۰۰ -> ۱۲۰۰۰۰
        make_promotion(self.product, percent=25)                                             # ۲۵٪ روی همان ۱۲۰۰۰۰
        breakdown = price_breakdown(self.product, None)
        self.assertEqual(breakdown.base, Decimal('120000'))
        self.assertEqual(breakdown.final, Decimal('90000'))
        reset_promotions_cache()


class GuestPricingHiddenModeTests(GuestPricingTestBase):
    """ حالت الف: مخفی‌سازی کامل قیمت؛ هیچ مبلغ خام/تخفیف‌خورده نباید در ساختار خروجی بماند """

    def test_final_price_and_base_price_tag_return_none(self):
        self.set_guest(mode='hide_price')
        self.assertIsNone(final_price(self.product, None))

    def test_breakdown_has_no_amounts_but_stays_a_valid_object(self):
        self.set_guest(mode='hide_price')
        breakdown = price_breakdown(self.product, None)
        self.assertFalse(breakdown.visible)
        self.assertIsNone(breakdown.base)
        self.assertIsNone(breakdown.final)
        self.assertEqual(breakdown.discount_amount, Decimal('0'))
        self.assertEqual(breakdown.has_discount, False)
        self.assertEqual(breakdown.percent, 0)

    def test_percent_is_preserved_even_though_amounts_are_hidden(self):
        self.set_guest(mode='hide_price')
        promo = make_promotion(self.product, percent=30)
        breakdown = price_breakdown(self.product, None)
        self.assertFalse(breakdown.visible)
        self.assertTrue(breakdown.has_discount)
        self.assertEqual(breakdown.percent, 30)
        self.assertIsNone(breakdown.base)
        self.assertIsNone(breakdown.final)
        reset_promotions_cache()

    def test_applied_promotions_carry_no_money_only_badge_and_timer_data(self):
        self.set_guest(mode='hide_price')
        make_promotion(self.product, percent=30, kind=Promotion.KIND_FIXED, value=45000, badge_label='شگفت‌انگیز')
        breakdown = price_breakdown(self.product, None)
        self.assertEqual(len(breakdown.applied), 1)
        applied = breakdown.applied[0]
        self.assertEqual(applied.discount, Decimal('0'))
        self.assertEqual(applied.value, 0)
        self.assertEqual(applied.badge_label, 'شگفت‌انگیز')          # غیرپولی: نشان تخفیف باید بماند
        reset_promotions_cache()

    def test_title_and_badge_label_are_blanked_only_when_a_non_percent_digit_is_present(self):
        """
        متن آزاد ادمین (عنوان/نشان) ممکن است مبلغ داخلش نوشته شده باشد؛ هر رقمی که با نماد درصد همراه نباشد
        (لاتین/فارسی/عربی) همان متن را پنهان می‌کند. رقمِ چسبیده به ٪/% بی‌خطر است (همان چیزی که percent هم
        دارد) و نباید باعث حذف نشانِ عمومیِ تخفیف شود. هر فیلد (عنوان/نشان) مستقل بر اساس محتوای خودش سنجیده
        می‌شود، نه بر اساس فیلد دیگر.
        """
        self.set_guest(mode='hide_price')
        cases = (
            ('تخفیف ۵۰ هزار تومانی', 'شگفت‌انگیز 99000', True, True),        # فارسی و لاتین، هر دو رقمِ مبلغی
            ('تخفیف ویژه', 'پیشنهاد شگفت‌انگیز', False, False),               # بدون هیچ رقمی؛ باید دست‌نخورده بماند
            ('۲۰٪ تخفیف ویژه', 'فقط امروز ٪۳۰', False, False),                # فقط رقمِ درصدی؛ دست‌نخورده بماند
            ('30% OFF today', 'حراج %25', False, False),                     # همان، با نماد لاتین
            ('۲۰٪ تخفیف، فقط ۵۰۰۰۰ تومان', 'ویژه', True, False),             # عنوان درصد+مبلغ پنهان شود؛ نشانِ بدون رقم مستقل بماند
        )
        for title, badge, title_hidden, badge_hidden in cases:
            with self.subTest(title=title):
                promo = make_promotion(self.product, percent=10, title=title, badge_label=badge)
                breakdown = price_breakdown(self.product, None)
                applied = breakdown.applied[0]
                self.assertEqual(applied.title, '' if title_hidden else title)
                self.assertEqual(applied.badge_label, '' if badge_hidden else badge)
                promo.delete()
                reset_promotions_cache()

    def test_no_amount_string_leaks_anywhere_in_repr_of_the_breakdown(self):
        """ اثبات مستقیم عدم نشت: هیچ عدد پولی واقعی (نه پایه، نه نهایی، نه تخفیف) در نمایش رشته‌ای ساختار نیست """
        self.set_guest(mode='hide_price')
        make_promotion(self.product, percent=30)
        breakdown = price_breakdown(self.product, None)
        text = repr(breakdown)
        for leaked in ('100000', '70000', '30000'):
            self.assertNotIn(leaked, text)
        reset_promotions_cache()

    def test_switching_back_to_visible_modes_restores_real_amounts(self):
        self.set_guest(mode='hide_price')
        self.assertIsNone(final_price(self.product, None))
        self.set_guest(mode='price_level', level=1)
        self.assertEqual(final_price(self.product, None), Decimal('100000'))


class GuestPricingAuthenticatedUserUnaffectedTests(GuestPricingTestBase):
    """ همه‌ی این تنظیمات فقط مهمان را تحت‌تأثیر می‌گذارد؛ کاربر واردشده هیچ‌وقت دست‌نخورده می‌ماند """

    def test_authenticated_user_ignores_every_guest_mode(self):
        user = CustomUser.objects.create_user(phone_number='09120009001', price_level=1)
        for mode, level, adj_type, adj_value in (
            ('hide_price', 1, 'percent', 0), ('price_level', 5, 'percent', 0),
            ('calculated_price', 1, 'percent', 90), ('calculated_price', 1, 'fixed', -500000),
        ):
            with self.subTest(mode=mode):
                self.set_guest(mode=mode, level=level, adj_type=adj_type, adj_value=adj_value)
                self.assertEqual(base_price(self.product, user), Decimal('100000'))
                breakdown = price_breakdown(self.product, user)
                self.assertTrue(breakdown.visible)


class GuestPricingPromotionEligibilityTests(PromotionTestMixin, TestCase):
    """ سطح مؤثر مهمان در تخفیف‌های خودکار: apply_to_vip باید دقیقاً مثل کاربر ویژه‌ی واقعی رفتار کند """

    @classmethod
    def setUpTestData(cls):
        cls.category = Category.objects.create(name='تست وفاداری مهمان', slug='guest-vip-cat')
        cls.product = Product.objects.create(
            name='کالای تست وفاداری مهمان', slug='guest-vip-product', erp_code='ERP-GUEST-VIP-1',
            category=cls.category, price=100000, price2=90000, price3=80000, stock=10,
        )

    def setUp(self):
        super().setUp()
        from products.models import SiteSettings
        self.SiteSettings = SiteSettings
        self.policy = DiscountPolicy.load()

    def set_guest_level(self, level):
        obj = self.SiteSettings.load()
        obj.guest_pricing_mode = 'price_level'
        obj.guest_price_level = level
        obj.full_clean()
        obj.save()

    def set_apply_to_vip(self, value):
        self.policy.apply_to_vip = value
        self.policy.save()

    def test_guest_below_vip_level_gets_automatic_discount_regardless_of_apply_to_vip(self):
        make_promotion(self.product, percent=20)
        for apply_to_vip in (False, True):
            with self.subTest(apply_to_vip=apply_to_vip):
                self.set_apply_to_vip(apply_to_vip)
                self.set_guest_level(1)
                self.assertEqual(final_price(self.product, None), Decimal('80000'))

    def test_guest_at_vip_level_follows_apply_to_vip_exactly_like_a_real_vip_user(self):
        make_promotion(self.product, percent=20)
        real_vip = CustomUser.objects.create_user(phone_number='09120009011', price_level=3)

        self.set_apply_to_vip(False)
        self.set_guest_level(3)
        self.assertEqual(final_price(self.product, None), Decimal('80000'))          # قیمت سطح ۳ (بدون تخفیف)
        self.assertEqual(final_price(self.product, real_vip), Decimal('80000'))      # کاربر واقعی هم همین‌طور

        self.set_apply_to_vip(True)
        self.assertEqual(final_price(self.product, None), Decimal('64000'))          # حالا ۲۰٪ روی ۸۰۰۰۰
        self.assertEqual(final_price(self.product, real_vip), Decimal('64000'))

    def test_price_levels_targeted_promotion_matches_guest_level_too(self):
        """ تخفیفی که فقط برای سطح‌های خاص تعریف شده، مهمانِ همان سطح را هم می‌بیند (نه فقط کاربر واردشده) """
        make_promotion(self.product, percent=15, price_levels='2')
        self.set_guest_level(1)
        self.assertEqual(final_price(self.product, None), Decimal('100000'))         # سطح ۱ مشمول نیست
        self.set_guest_level(2)
        self.assertEqual(final_price(self.product, None), Decimal('76500'))          # ۹۰۰۰۰ × ۰٫۸۵


class GuestPricingCacheTests(GuestPricingTestBase):
    """ پرفورمنس: کوئری اضافه به ازای هر محصول نباید بخورد؛ memo با ذخیره‌ی ادمین فوراً باطل می‌شود """

    def test_no_extra_database_query_per_product_after_warmup(self):
        from promotions.index import get_index
        self.SiteSettings.cached()                                 # گرم‌کردن کش تنظیمات مهمان (خارج از شمارش)
        get_index()                                                 # گرم‌کردن شاخص تخفیف‌های خودکار (خارج از شمارش)
        products = [
            Product.objects.create(name=f'کالای {i}', slug=f'guest-cache-{i}', erp_code=f'ERP-GC-{i}',
                                   category=self.category, price=100000 + i, stock=5)
            for i in range(3)
        ]
        with self.assertNumQueries(0):
            for product in products:
                price_breakdown(product, None)

    def test_admin_save_invalidates_the_memo_immediately_not_after_two_seconds(self):
        self.assertEqual(base_price(self.product, None), Decimal('100000'))         # memo با سطح ۱ گرم می‌شود
        self.set_guest(level=2)                                                      # save() واقعی؛ سیگنال باید memo را پاک کند
        self.assertEqual(base_price(self.product, None), Decimal('90000'))          # بدون صبر، همان لحظه دیده می‌شود


class ProductCardGuestPricingTemplateTests(GuestPricingTestBase):
    """ فاز ۳: رندر واقعی قالب‌ها (کارت، جزئیات، جستجوی زنده، پنل فیلتر) برای مهمان در هر سه حالت """

    def _list_response(self):
        return self.client.get(reverse('products:product_list'))

    def _detail_response(self):
        return self.client.get(reverse('products:product_detail', args=[self.product.slug]))

    def test_hidden_mode_card_shows_cta_not_a_price(self):
        self.set_guest(mode='hide_price')
        response = self._list_response()
        self.assertContains(response, 'guest-hidden-price-box')
        self.assertContains(response, GUEST_HIDDEN_MESSAGE_DEFAULT)
        self.assertNotContains(response, '100000')

    def test_hidden_mode_custom_admin_message_is_used(self):
        self.set_guest(mode='hide_price', message='پیام دلخواه ادمین برای مهمان')
        response = self._list_response()
        self.assertContains(response, 'پیام دلخواه ادمین برای مهمان')

    def test_hidden_mode_with_discount_shows_only_the_percent_badge(self):
        self.set_guest(mode='hide_price')
        make_promotion(self.product, percent=25)
        response = self._list_response()
        self.assertContains(response, '٪25')
        self.assertNotContains(response, '100000')
        self.assertNotContains(response, '75000')
        reset_promotions_cache()

    def test_visible_modes_show_the_real_price_and_never_the_cta_box(self):
        for mode, level, adj_type, adj_value in (
            ('price_level', 2, 'percent', 0), ('calculated_price', 1, 'percent', 10),
        ):
            with self.subTest(mode=mode):
                self.set_guest(mode=mode, level=level, adj_type=adj_type, adj_value=adj_value)
                response = self._list_response()
                self.assertNotContains(response, 'guest-hidden-price-box')

    def test_product_detail_page_hidden_mode_replaces_price_and_buy_button(self):
        self.set_guest(mode='hide_price')
        response = self._detail_response()
        self.assertContains(response, 'guest-hidden-price-box')
        self.assertContains(response, 'ورود / ثبت‌نام')
        self.assertNotContains(response, '100000')
        self.assertNotContains(response, 'مشاهده سبد خرید')

    def test_product_detail_page_visible_mode_shows_price_and_no_cta_box(self):
        self.set_guest(mode='price_level', level=1)
        response = self._detail_response()
        self.assertContains(response, '100000')
        self.assertNotContains(response, 'guest-hidden-price-box')

    def test_live_search_reads_price_from_the_pricing_engine_not_the_raw_field(self):
        """ قبلاً product.price خام نشان داده می‌شد؛ حالا با تعدیل مهمان هم باید هماهنگ باشد """
        self.set_guest(mode='calculated_price', level=1, adj_type='fixed', adj_value=5000)
        response = self.client.get(reverse('products:live_search'), {'q': self.product.name[:8]})
        self.assertContains(response, '105000')

    def test_live_search_hides_price_in_hidden_mode(self):
        self.set_guest(mode='hide_price')
        response = self.client.get(reverse('products:live_search'), {'q': self.product.name[:8]})
        self.assertContains(response, 'ورود برای مشاهده قیمت')
        self.assertNotContains(response, '100000')

    def test_filter_panel_hides_the_price_range_only_in_hidden_mode(self):
        self.set_guest(mode='hide_price')
        response = self._list_response()
        self.assertNotContains(response, 'محدوده قیمت')
        self.set_guest(mode='price_level', level=1)
        response = self._list_response()
        self.assertContains(response, 'محدوده قیمت')

    def test_authenticated_user_never_sees_the_hidden_box_regardless_of_guest_mode(self):
        """ حتی وقتی سایت در حالت «مخفی‌سازی قیمت» برای مهمان است، کاربر واردشده تحت‌تأثیر قرار نمی‌گیرد """
        user = CustomUser.objects.create_user(phone_number='09120009021', price_level=1)
        self.client.force_login(user)
        self.set_guest(mode='hide_price')
        response = self._detail_response()
        self.assertNotContains(response, 'guest-hidden-price-box')
        self.assertContains(response, '100000')


class EffectivePriceFilterSortTests(TestCase):
    """
    فاز ۴: فیلتر بازه‌ی قیمت (price_min/price_max) و مرتب‌سازی ارزان‌ترین/گران‌ترین در فروشگاه بر اساس قیمت
    مؤثرِ همان کاربر/مهمان (سطح قیمت/روش پرداخت، و برای مهمان تعدیل فرمولی)، نه فیلد خام Product.price.

    دو محصول عمداً طوری ساخته شده‌اند که ترتیب سطح ۱ با ترتیب سطح ۲/۳ *معکوس* است، تا اثبات شود فیلتر/
    مرتب‌سازی واقعاً قیمت هر سطح را جدا می‌خواند، نه همیشه price را.
    """

    @classmethod
    def setUpTestData(cls):
        cls.category = Category.objects.create(name='تست فاز۴', slug='phase4-price-cat')
        cls.a = Product.objects.create(
            name='محصول آ', slug='phase4-product-a', erp_code='ERP-P4-A',
            category=cls.category, price=50000, price2=200000, price3=90000, stock=5,
        )
        cls.b = Product.objects.create(
            name='محصول ب', slug='phase4-product-b', erp_code='ERP-P4-B',
            category=cls.category, price=150000, price2=20000, price3=10000, stock=5,
        )
        cls.cash_user = CustomUser.objects.create_user(phone_number='09121234001', price_level=2)
        cls.vip_user = CustomUser.objects.create_user(phone_number='09121234002', price_level=3)

    def setUp(self):
        SiteSettings.load().save()

    def _list(self, **params):
        params.setdefault('category', self.category.slug)
        return self.client.get(reverse('products:product_list'), params)

    def _ab_order(self, response):
        return [p.pk for p in response.context['products'] if p.pk in (self.a.pk, self.b.pk)]

    def test_default_guest_sort_matches_level_one_like_before(self):
        response = self._list(sort='price_asc')
        self.assertEqual(self._ab_order(response), [self.a.pk, self.b.pk])          # ۵۰۰۰۰ < ۱۵۰۰۰۰

    def test_cash_user_sort_is_reversed_compared_to_level_one(self):
        self.client.force_login(self.cash_user)
        response = self._list(sort='price_asc')
        self.assertEqual(self._ab_order(response), [self.b.pk, self.a.pk])          # ب:۲۰۰۰۰ < آ:۲۰۰۰۰۰

    def test_vip_user_sort_uses_their_own_price_level(self):
        self.client.force_login(self.vip_user)
        response = self._list(sort='price_desc')
        self.assertEqual(self._ab_order(response), [self.a.pk, self.b.pk])          # نزولی: آ:۹۰۰۰۰ قبل از ب:۱۰۰۰۰

    def test_price_range_filter_uses_the_cash_users_own_price(self):
        self.client.force_login(self.cash_user)
        response = self._list(price_min='15000', price_max='25000')
        ids = self._ab_order(response)
        self.assertIn(self.b.pk, ids)          # ب: نقدی ۲۰۰۰۰، داخل بازه
        self.assertNotIn(self.a.pk, ids)       # آ: نقدی ۲۰۰۰۰۰، خارج بازه

    def test_price_bounds_reflect_the_logged_in_users_own_price(self):
        self.client.force_login(self.cash_user)
        response = self._list()
        bounds = response.context['price_bounds']
        self.assertEqual(int(bounds['min_price']), 20000)   # کمینه‌ی نقدی بین همه‌ی محصولات مرئی
        self.assertLessEqual(int(bounds['min_price']), 20000)

    def test_guest_calculated_mode_filter_uses_the_adjusted_price(self):
        obj = SiteSettings.load()
        obj.guest_pricing_mode = 'calculated_price'
        obj.guest_price_level = 1
        obj.guest_adjustment_type = 'fixed'
        obj.guest_adjustment_value = 100000
        obj.guest_price_rounding_step = 1
        obj.full_clean()
        obj.save()
        # آ: ۵۰۰۰۰+۱۰۰۰۰۰=۱۵۰۰۰۰ (داخل بازه)، ب: ۱۵۰۰۰۰+۱۰۰۰۰۰=۲۵۰۰۰۰ (خارج بازه)
        response = self._list(price_min='140000', price_max='160000')
        ids = self._ab_order(response)
        self.assertIn(self.a.pk, ids)
        self.assertNotIn(self.b.pk, ids)

    def test_hidden_mode_ignores_price_params_from_the_url(self):
        obj = SiteSettings.load()
        obj.guest_pricing_mode = 'hide_price'
        obj.full_clean()
        obj.save()
        blocked = self._list(price_min='1000000', price_max='2000000', sort='price_asc')
        unfiltered = self._list()
        self.assertIsNone(blocked.context['price_min'])
        self.assertIsNone(blocked.context['price_max'])
        self.assertEqual(blocked.context['current_sort'], 'newest')
        self.assertEqual(
            {p.pk for p in blocked.context['products']}, {p.pk for p in unfiltered.context['products']},
        )

    def test_hidden_mode_price_bounds_are_neutral(self):
        obj = SiteSettings.load()
        obj.guest_pricing_mode = 'hide_price'
        obj.full_clean()
        obj.save()
        response = self._list()
        self.assertEqual(response.context['price_bounds'], {'min_price': None, 'max_price': None})

    def test_authenticated_user_ignores_the_hidden_mode_switch_entirely(self):
        obj = SiteSettings.load()
        obj.guest_pricing_mode = 'hide_price'
        obj.full_clean()
        obj.save()
        self.client.force_login(self.cash_user)
        response = self._list(price_min='15000', price_max='25000', sort='price_asc')
        self.assertEqual(response.context['price_min'], 15000)
        self.assertEqual(response.context['current_sort'], 'price_asc')

    def test_effective_price_filter_adds_no_extra_query_roundtrip(self):
        """
        annotate یک ستون به همان SELECT اضافه می‌کند، نه یک رفت‌وبرگشتِ جدا به دیتابیس. یک بازدید اول برای
        گرم‌شدن کش‌ها (شاخص تخفیف، SiteSettings، دسته‌های وبلاگ...) لازم است، وگرنه اختلاف کوئریِ ناشی از
        سردی/گرمیِ کش با اختلاف واقعیِ ناشی از annotate قاطی می‌شود.
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        self.client.force_login(self.vip_user)
        self._list()                                       # گرم‌کردن کش‌ها (خارج از شمارش)
        with CaptureQueriesContext(connection) as without_filter:
            self._list()
        with CaptureQueriesContext(connection) as with_filter:
            self._list(price_min='1000', price_max='9000000', sort='price_asc')
        self.assertEqual(len(with_filter.captured_queries), len(without_filter.captured_queries))
