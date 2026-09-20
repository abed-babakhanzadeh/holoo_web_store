"""
تست ماتریس تصمیمِ تابع خالص shipping_quote (orders/shipping.py).

کالاها و تنظیمات با شیءهای ساده جایگزین می‌شوند (تابع فقط چند فیلد را می‌خواند) و آدرس‌ها ذخیره‌نشده‌اند،
مگر در تست‌های انتها که با مدل‌های واقعی هم همین قواعد را می‌آزمایند.
"""

import dataclasses
from types import SimpleNamespace

from django.test import TestCase

from accounts.models import Address, CustomUser
from locations.models import City, DeliveryZone, Province
from orders import shipping
from orders.shipping import ShippingQuote, shipping_quote
from products.models import SiteSettings


def product(free=False):
    return SimpleNamespace(free_shipping=free)


def policy(**overrides):
    data = dict(courier_free_for_free_shipping_cart=True, postage_collect_enabled=True,
                postage_collect_label='پس‌کرایه (پرداخت هزینه درب منزل)',
                postage_disabled_message='امکان ارسال به این شهر فعلاً وجود ندارد.')
    data.update(overrides)
    return SimpleNamespace(**data)


class ShippingQuoteBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.province = Province.objects.create(name='استان آزمون حمل')
        cls.zoned_city = City.objects.create(province=cls.province, name='شهر پیکیِ آزمون')
        cls.zone = DeliveryZone.objects.create(city=cls.zoned_city, name='ناحیه‌ی آزمون', shipping_cost=45000)
        cls.unpriced_zone = DeliveryZone.objects.create(city=cls.zoned_city, name='ناحیه‌ی بدون تعرفه', shipping_cost=0)
        cls.post_city = City.objects.create(province=cls.province, name='شهر پستیِ آزمون')

    def address(self, city=None, zone=None):
        """ آدرس ذخیره‌نشده (تابع فقط city/zone را می‌خواند) """
        return Address(city=city or self.post_city, zone=zone)


class FreeShippingCartTests(ShippingQuoteBase):
    """ ۱. سبدِ «ارسال رایگان» با سیاست روشن در برابر خاموش """

    def test_free_cart_with_flag_on_makes_courier_free(self):
        quote = shipping_quote(self.address(self.zoned_city, self.zone), [product(True), product(True)], policy())
        self.assertEqual((quote.available, quote.method, quote.cost), (True, 'courier', 0))
        self.assertEqual(quote.label, shipping.LABEL_COURIER_FREE)
        self.assertTrue(quote.free_cart)

    def test_free_cart_with_flag_off_keeps_the_zone_tariff(self):
        quote = shipping_quote(self.address(self.zoned_city, self.zone), [product(True)],
                               policy(courier_free_for_free_shipping_cart=False))
        self.assertEqual((quote.available, quote.method, quote.cost), (True, 'courier', 45000))
        self.assertEqual(quote.label, shipping.LABEL_COURIER)
        self.assertTrue(quote.free_cart)                    # سبد رایگان است ولی سیاست سایت اجازه‌ی صفر شدن پیک را نمی‌دهد

    def test_one_non_free_product_means_full_courier_fee_even_with_flag_on(self):
        quote = shipping_quote(self.address(self.zoned_city, self.zone), [product(True), product(False)], policy())
        self.assertEqual((quote.method, quote.cost), ('courier', 45000))
        self.assertFalse(quote.free_cart)

    def test_empty_cart_is_never_free(self):
        quote = shipping_quote(self.address(self.zoned_city, self.zone), [], policy())
        self.assertEqual(quote.cost, 45000)
        self.assertFalse(quote.free_cart)

    def test_free_cart_to_a_postage_city_stays_zero_and_postage_collect(self):
        for flag in (True, False):
            with self.subTest(flag=flag):
                quote = shipping_quote(self.address(self.post_city), [product(True)],
                                       policy(courier_free_for_free_shipping_cart=flag))
                self.assertEqual((quote.available, quote.method, quote.cost), (True, 'post', 0))

    def test_unset_tariff_outranks_free_shipping_in_every_combination(self):
        """ ناحیه‌ای که تعرفه ندارد هنوز برای ارسال با پیک تأیید نشده؛ نه سبد رایگان و نه سیاست روشن نباید آن را دور بزنند """
        for free_cart in (True, False):
            for flag in (True, False):
                with self.subTest(free_cart=free_cart, flag=flag):
                    quote = shipping_quote(self.address(self.zoned_city, self.unpriced_zone), [product(free_cart)],
                                           policy(courier_free_for_free_shipping_cart=flag))
                    self.assertFalse(quote.available)
                    self.assertEqual(quote.reason, shipping.REASON_TARIFF_UNSET)
                    self.assertEqual(quote.message, shipping.MSG_TARIFF_UNSET)
                    self.assertEqual((quote.method, quote.cost, quote.label), ('', 0, ''))

    def test_free_cart_becomes_free_only_after_the_zone_gets_a_tariff(self):
        address = self.address(self.zoned_city, self.unpriced_zone)
        self.assertFalse(shipping_quote(address, [product(True)], policy()).available)
        self.unpriced_zone.shipping_cost = 30000                    # تعرفه ثبت شد ← همان لحظه رایگان‌شدنِ سبد اعمال می‌شود
        quote = shipping_quote(address, [product(True)], policy())
        self.assertEqual((quote.available, quote.method, quote.cost), (True, 'courier', 0))
        quote = shipping_quote(address, [product(True)], policy(courier_free_for_free_shipping_cart=False))
        self.assertEqual(quote.cost, 30000)


class ZoneWithTariffTests(ShippingQuoteBase):
    """ ۲. آدرس ناحیه‌دار (مثل قم/پردیسان) با تعرفه‌ی معتبر """

    def test_courier_with_the_zone_tariff(self):
        quote = shipping_quote(self.address(self.zoned_city, self.zone), [product(False)], policy())
        self.assertEqual(quote, ShippingQuote(available=True, method='courier', cost=45000,
                                              label='ارسال با پیک', reason='', message='', free_cart=False))
        self.assertTrue(quote.is_courier)
        self.assertFalse(quote.is_postage_collect)

    def test_each_zone_uses_its_own_tariff(self):
        other = DeliveryZone.objects.create(city=self.zoned_city, name='ناحیه‌ی دیگر', shipping_cost=90000)
        self.assertEqual(shipping_quote(self.address(self.zoned_city, self.zone), [product()], policy()).cost, 45000)
        self.assertEqual(shipping_quote(self.address(self.zoned_city, other), [product()], policy()).cost, 90000)

    def test_cost_is_a_plain_int(self):
        quote = shipping_quote(self.address(self.zoned_city, self.zone), [product()], policy())
        self.assertIs(type(quote.cost), int)


class UnsetTariffTests(ShippingQuoteBase):
    """ ۳. ناحیه‌ی با تعرفه‌ی صفر/تنظیم‌نشده: مسدود با پیام مشخص """

    def test_zero_tariff_blocks_with_the_exact_message(self):
        quote = shipping_quote(self.address(self.zoned_city, self.unpriced_zone), [product(False)], policy())
        self.assertFalse(quote.available)
        self.assertEqual(quote.reason, 'tariff_unset')
        self.assertEqual(quote.message, 'تعرفه ارسال به این ناحیه هنوز تعیین نشده است؛ لطفاً با پشتیبانی تماس بگیرید.')
        self.assertEqual((quote.method, quote.cost, quote.label), ('', 0, ''))

    def test_setting_a_tariff_unblocks_it(self):
        address = self.address(self.zoned_city, self.unpriced_zone)
        self.assertFalse(shipping_quote(address, [product()], policy()).available)
        self.unpriced_zone.shipping_cost = 30000
        self.assertEqual(shipping_quote(address, [product()], policy()).cost, 30000)

    def test_inactive_zone_is_blocked(self):
        inactive = DeliveryZone.objects.create(city=self.zoned_city, name='ناحیه‌ی غیرفعال', shipping_cost=1000, is_active=False)
        quote = shipping_quote(self.address(self.zoned_city, inactive), [product(True)], policy())
        self.assertFalse(quote.available)
        self.assertEqual(quote.reason, 'zone_inactive')
        self.assertEqual(quote.message, shipping.MSG_ZONE_INACTIVE)


class ZonedCityWithoutZoneTests(ShippingQuoteBase):
    """ ۴. شهر ناحیه‌دار ولی آدرس بدون ناحیه (مثل آدرسِ منتقل‌شده‌ی قدیمی) """

    def test_blocked_and_asks_for_a_zone(self):
        quote = shipping_quote(self.address(self.zoned_city, None), [product(False)], policy())
        self.assertFalse(quote.available)
        self.assertEqual(quote.reason, 'zone_required')
        self.assertEqual(quote.message, shipping.MSG_ZONE_REQUIRED)
        self.assertEqual((quote.method, quote.cost), ('', 0))

    def test_blocked_even_for_free_cart_and_postage_disabled(self):
        for overrides in ({}, {'postage_collect_enabled': False}):
            with self.subTest(overrides=overrides):
                quote = shipping_quote(self.address(self.zoned_city, None), [product(True)], policy(**overrides))
                self.assertEqual(quote.reason, 'zone_required')                # «پست» راهِ فرار از انتخاب ناحیه نیست

    def test_inactive_zones_do_not_make_a_city_zoned(self):
        city = City.objects.create(province=self.province, name='شهر با ناحیه‌ی غیرفعال')
        DeliveryZone.objects.create(city=city, name='ناحیه', shipping_cost=1000, is_active=False)
        quote = shipping_quote(self.address(city, None), [product()], policy())
        self.assertEqual((quote.available, quote.method), (True, 'post'))


class PostageCollectTests(ShippingQuoteBase):
    """ ۵. سایر شهرها با پس‌کرایه‌ی فعال """

    def test_post_with_zero_cost_and_the_configured_label(self):
        quote = shipping_quote(self.address(self.post_city), [product(False)], policy())
        self.assertEqual(quote, ShippingQuote(available=True, method='post', cost=0,
                                              label='پس‌کرایه (پرداخت هزینه درب منزل)', reason='', message='', free_cart=False))
        self.assertTrue(quote.is_postage_collect)

    def test_label_comes_from_the_settings(self):
        quote = shipping_quote(self.address(self.post_city), [product()], policy(postage_collect_label='کرایه‌ی پست با گیرنده'))
        self.assertEqual(quote.label, 'کرایه‌ی پست با گیرنده')


class PostageDisabledTests(ShippingQuoteBase):
    """ ۶. سایر شهرها با پس‌کرایه‌ی غیرفعال: مسدود با پیام تنظیمات """

    def test_blocked_with_the_settings_message(self):
        quote = shipping_quote(self.address(self.post_city), [product(False)], policy(postage_collect_enabled=False))
        self.assertFalse(quote.available)
        self.assertEqual(quote.reason, 'postage_disabled')
        self.assertEqual(quote.message, 'امکان ارسال به این شهر فعلاً وجود ندارد.')
        self.assertEqual((quote.method, quote.cost, quote.label), ('', 0, ''))

    def test_message_is_taken_from_the_settings(self):
        quote = shipping_quote(self.address(self.post_city), [product()],
                               policy(postage_collect_enabled=False, postage_disabled_message='فعلاً فقط داخل قم ارسال داریم.'))
        self.assertEqual(quote.message, 'فعلاً فقط داخل قم ارسال داریم.')

    def test_disabling_postage_does_not_affect_courier_zones(self):
        quote = shipping_quote(self.address(self.zoned_city, self.zone), [product()], policy(postage_collect_enabled=False))
        self.assertEqual((quote.available, quote.method, quote.cost), (True, 'courier', 45000))


class NoAddressAndPurityTests(ShippingQuoteBase):
    def test_no_address_is_blocked(self):
        quote = shipping_quote(None, [product(True)], policy())
        self.assertFalse(quote.available)
        self.assertEqual((quote.reason, quote.message), ('no_address', shipping.MSG_NO_ADDRESS))

    def test_quote_is_immutable_and_inputs_are_not_touched(self):
        address = self.address(self.zoned_city, self.zone)
        products = [product(True)]
        settings_obj = policy()
        before = (dataclasses.asdict(shipping_quote(address, products, settings_obj)), dict(vars(settings_obj)))
        quote = shipping_quote(address, products, settings_obj)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            quote.cost = 1
        self.assertEqual((dataclasses.asdict(quote), dict(vars(settings_obj))), before)      # قطعی و بدون اثر جانبی
        self.assertEqual(len(products), 1)

    def test_quote_has_the_documented_structure(self):
        quote = shipping_quote(self.address(self.post_city), [product()], policy())
        self.assertEqual([f.name for f in dataclasses.fields(quote)],
                         ['available', 'method', 'cost', 'label', 'reason', 'message', 'free_cart'])

    def test_available_quotes_never_carry_a_reason_and_blocked_ones_never_a_cost(self):
        cases = [
            (self.address(self.zoned_city, self.zone), policy()),
            (self.address(self.zoned_city, self.unpriced_zone), policy()),
            (self.address(self.zoned_city, None), policy()),
            (self.address(self.post_city), policy()),
            (self.address(self.post_city), policy(postage_collect_enabled=False)),
            (None, policy()),
        ]
        for address, pol in cases:
            with self.subTest(address=address):
                quote = shipping_quote(address, [product()], pol)
                if quote.available:
                    self.assertEqual((quote.reason, quote.message), ('', ''))
                    self.assertIn(quote.method, shipping.SHIPPING_METHODS)
                else:
                    self.assertEqual((quote.method, quote.cost, quote.label), ('', 0, ''))
                    self.assertTrue(quote.reason and quote.message)


class RealModelsTests(TestCase):
    """ همان قواعد با Address/SiteSettings/محصولِ واقعی و ناحیه‌ی بذرِ قم (پردیسان) """

    def setUp(self):
        self.user = CustomUser.objects.create_user(phone_number='09120006001', first_name='علی', last_name='رضایی')
        self.settings = SiteSettings.load()
        qom = City.objects.get(province__name='قم', name='قم')
        self.pardisan = DeliveryZone.objects.get(city=qom, name='پردیسان')
        self.kahak_city = City.objects.get(province__name='قم', name='کهک')      # شهر جدا؛ ناحیه ندارد ← پست
        self.qom = qom

    def _address(self, city, zone=None):
        return Address.objects.create(user=self.user, title='آزمون', receiver_first_name='علی', receiver_last_name='رضایی',
                                      receiver_phone='09121112233', city=city, zone=zone, postal_code='1234567890', address='خیابان آزمون')

    def _product(self, free):
        from products.models import Category, Product
        category, _ = Category.objects.get_or_create(slug='shipping-quote-cat', defaults={'name': 'آزمون حمل'})
        return Product.objects.create(name='کالا', slug=f'shipping-quote-{Product.objects.count()}', erp_code=f'ERP-SQ-{Product.objects.count()}',
                                      category=category, price=100000, stock=5, free_shipping=free)

    def test_seeded_qom_zone_is_blocked_until_admin_sets_a_tariff(self):
        address = self._address(self.qom, self.pardisan)
        quote = shipping_quote(address, [self._product(False)], self.settings)
        self.assertEqual(quote.reason, 'tariff_unset')                                # تعرفه‌ی اولیه ۰ = تنظیم‌نشده
        DeliveryZone.objects.filter(pk=self.pardisan.pk).update(shipping_cost=60000)
        address = Address.objects.get(pk=address.pk)
        quote = shipping_quote(address, [self._product(False)], self.settings)
        self.assertEqual((quote.method, quote.cost), ('courier', 60000))

    def test_other_city_in_the_same_province_goes_by_post(self):
        quote = shipping_quote(self._address(self.kahak_city), [self._product(False)], self.settings)
        self.assertEqual((quote.available, quote.method, quote.cost), (True, 'post', 0))
        self.assertEqual(quote.label, self.settings.postage_collect_label)

    def test_legacy_qom_address_without_zone_is_blocked(self):
        address = self._address(self.kahak_city)
        Address.objects.filter(pk=address.pk).update(city=self.qom)                    # مثل آدرس منتقل‌شده‌ی قدیمی
        quote = shipping_quote(Address.objects.get(pk=address.pk), [self._product(False)], self.settings)
        self.assertEqual(quote.reason, 'zone_required')

    def test_real_settings_flags_are_honoured(self):
        DeliveryZone.objects.filter(pk=self.pardisan.pk).update(shipping_cost=60000)
        address = Address.objects.get(pk=self._address(self.qom, DeliveryZone.objects.get(pk=self.pardisan.pk)).pk)
        free = [self._product(True)]
        self.assertEqual(shipping_quote(address, free, self.settings).cost, 0)
        self.settings.courier_free_for_free_shipping_cart = False
        self.assertEqual(shipping_quote(address, free, self.settings).cost, 60000)
        self.settings.postage_collect_enabled = False
        post_address = self._address(self.kahak_city)
        self.assertEqual(shipping_quote(post_address, free, self.settings).reason, 'postage_disabled')
