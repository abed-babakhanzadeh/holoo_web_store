"""
تست قاعده‌های ارسال رایگان (FreeShippingRule): اسنپ‌شات و شروط (حداقل سبد، بازه‌ی زمانی، محدوده‌ی جغرافیایی)، لودر و کش،
و یکپارچگی مستقیم با تابع خالص shipping_quote (پیک/پست، تعرفه‌ی صفر، اولویت، برچسب).
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from accounts.models import Address
from locations.models import City, DeliveryZone, Province
from orders import shipping
from orders.shipping import shipping_quote, waive_shipping

from . import free_shipping
from .free_shipping import FreeShippingRuleSnapshot, build_rules, get_rules
from .models import FreeShippingRule
from .testing import PromotionTestMixin, make_free_shipping_rule


def policy(**overrides):
    data = dict(courier_free_for_free_shipping_cart=True, postage_collect_enabled=True,
                postage_collect_label='پس‌کرایه (پرداخت هزینه درب منزل)', postage_disabled_message='ارسال فعلاً ممکن نیست.')
    data.update(overrides)
    return SimpleNamespace(**data)


def product(free=False):
    return SimpleNamespace(free_shipping=free)


class FreeShippingBase(PromotionTestMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.qom = Province.objects.create(name='قم آزمون')
        cls.tehran = Province.objects.create(name='تهران آزمون')
        cls.qom_city = City.objects.create(province=cls.qom, name='قم شهر')
        cls.kahak = City.objects.create(province=cls.qom, name='کهک')
        cls.tehran_city = City.objects.create(province=cls.tehran, name='تهران شهر')
        cls.shiraz = City.objects.create(province=Province.objects.create(name='فارس آزمون'), name='شیراز')
        cls.zone = DeliveryZone.objects.create(city=cls.qom_city, name='پردیسان', shipping_cost=45000)
        cls.unpriced = DeliveryZone.objects.create(city=cls.qom_city, name='بدون تعرفه', shipping_cost=0)

    def address(self, city, zone=None):
        return Address(city=city, zone=zone)

    def quote(self, address, rules, cart_total, now=None, site=None, products=None):
        return shipping_quote(address, products or [product()], site or policy(), cart_total=cart_total, free_rules=rules, now=now)


# ======================================================================== اسنپ‌شات
class SnapshotTests(FreeShippingBase):
    def snap(self, **overrides):
        data = dict(id=1, title='آزمون', min_total=0, starts_at=None, ends_at=None, priority=0,
                    province_ids=frozenset(), city_ids=frozenset(), postage_mode='ignore')
        data.update(overrides)
        return FreeShippingRuleSnapshot(**data)

    def test_minimum_is_inclusive(self):
        rule = self.snap(min_total=2_000_000)
        now = timezone.now()
        self.assertFalse(rule.matches(self.qom_city, 1_999_999, now))
        self.assertTrue(rule.matches(self.qom_city, 2_000_000, now))
        self.assertTrue(rule.matches(self.qom_city, 5_000_000, now))

    def test_zero_minimum_matches_any_total_including_zero(self):
        self.assertTrue(self.snap().matches(self.qom_city, 0, timezone.now()))

    def test_window_is_inclusive_and_open_ended_sides_are_allowed(self):
        now = timezone.now()
        rule = self.snap(starts_at=now, ends_at=now + timedelta(days=1))
        self.assertTrue(rule.in_window(now))
        self.assertTrue(rule.in_window(now + timedelta(days=1)))
        self.assertFalse(rule.in_window(now - timedelta(microseconds=1)))
        self.assertFalse(rule.in_window(now + timedelta(days=1, microseconds=1)))
        self.assertTrue(self.snap(starts_at=now).in_window(now + timedelta(days=999)))
        self.assertTrue(self.snap(ends_at=now).in_window(now - timedelta(days=999)))
        self.assertTrue(self.snap().in_window(now))

    def test_geography(self):
        nationwide = self.snap()
        self.assertTrue(nationwide.nationwide)
        self.assertTrue(nationwide.covers_city(self.shiraz))
        by_province = self.snap(province_ids=frozenset({self.qom.pk}))
        self.assertTrue(by_province.covers_city(self.qom_city) and by_province.covers_city(self.kahak))
        self.assertFalse(by_province.covers_city(self.tehran_city))
        by_city = self.snap(city_ids=frozenset({self.qom_city.pk}))
        self.assertTrue(by_city.covers_city(self.qom_city))
        self.assertFalse(by_city.covers_city(self.kahak))                   # همان استان ولی شهر دیگر
        both = self.snap(province_ids=frozenset({self.tehran.pk}), city_ids=frozenset({self.qom_city.pk}))
        self.assertTrue(both.covers_city(self.qom_city) and both.covers_city(self.tehran_city))
        self.assertFalse(both.covers_city(self.kahak))

    def test_labels(self):
        self.assertEqual(self.snap(min_total=2000000).label(), 'ارسال رایگان (خرید بالای 2000000 تومان)')
        self.assertEqual(self.snap(min_total=2000000).post_label(), 'ارسال رایگان با پست (خرید بالای 2000000 تومان)')
        self.assertEqual(self.snap(title='عید نوروز').label(), 'ارسال رایگان (عید نوروز)')

    def test_covers_postage_flag(self):
        self.assertFalse(self.snap().covers_postage)
        self.assertTrue(self.snap(postage_mode='cover').covers_postage)

    def test_snapshots_are_picklable_for_redis(self):
        import pickle
        rule = self.snap(min_total=5, province_ids=frozenset({1, 2}))
        self.assertEqual(pickle.loads(pickle.dumps(rule)), rule)


# ======================================================================== لودر و کش
class LoaderTests(FreeShippingBase):
    def test_no_rules(self):
        self.assertEqual(build_rules(), ())
        self.assertEqual(get_rules(), ())

    def test_build_reads_fields_geography_and_orders_by_priority(self):
        low = make_free_shipping_rule('کم‌اولویت', min_total=100, priority=1)
        high = make_free_shipping_rule('پراولویت', min_total=500, provinces=[self.qom], cities=[self.tehran_city], priority=5,
                                       postage_mode='cover')
        rules = build_rules()
        self.assertEqual([r.id for r in rules], [high.pk, low.pk])
        self.assertEqual((rules[0].min_total, rules[0].province_ids, rules[0].city_ids, rules[0].postage_mode),
                         (500, frozenset({self.qom.pk}), frozenset({self.tehran_city.pk}), 'cover'))
        self.assertTrue(rules[1].nationwide)

    def test_inactive_and_long_expired_rules_are_not_loaded_but_scheduled_ones_are(self):
        make_free_shipping_rule('غیرفعال', active=False)
        make_free_shipping_rule('منقضی', ends_at=timezone.now() - timedelta(days=2), starts_at=timezone.now() - timedelta(days=5))
        make_free_shipping_rule('آینده', scheduled=True)
        self.assertEqual([r.title for r in build_rules()], ['آینده'])

    def test_build_uses_three_queries_regardless_of_rule_count(self):
        for i in range(6):
            make_free_shipping_rule(f'قاعده {i}', provinces=[self.qom], cities=[self.tehran_city])
        with self.assertNumQueries(3):
            self.assertEqual(len(build_rules()), 6)

    def test_warm_cache_needs_no_queries(self):
        make_free_shipping_rule('گرم')
        get_rules()
        with self.assertNumQueries(0):
            get_rules()

    def test_every_change_invalidates_including_many_to_many(self):
        rule = make_free_shipping_rule('باطل‌سازی', min_total=100)
        self.assertEqual(get_rules()[0].min_total, 100)
        rule.min_cart_total = 900
        rule.save()
        self.assertEqual(get_rules()[0].min_total, 900)
        rule.provinces.add(self.qom)                                          # فقط M2M عوض شد
        self.assertEqual(get_rules()[0].province_ids, frozenset({self.qom.pk}))
        rule.cities.add(self.shiraz)
        self.assertEqual(get_rules()[0].city_ids, frozenset({self.shiraz.pk}))
        rule.provinces.remove(self.qom)
        self.assertEqual(get_rules()[0].province_ids, frozenset())
        rule.delete()
        self.assertEqual(get_rules(), ())

    def test_cache_key_contains_the_database_name(self):
        from django.db import connection
        self.assertIn(connection.settings_dict['NAME'], free_shipping.cache_key())

    def test_redis_outage_falls_back_to_the_database(self):
        make_free_shipping_rule('بدون ردیس')
        free_shipping.invalidate()
        with mock.patch.object(free_shipping.cache, 'get', side_effect=ConnectionError('down')), \
                mock.patch.object(free_shipping.cache, 'set', side_effect=ConnectionError('down')):
            self.assertEqual(len(get_rules()), 1)
        with mock.patch.object(free_shipping.cache, 'delete', side_effect=ConnectionError('down')):
            free_shipping.invalidate()

    def test_scheduled_rule_becomes_effective_with_time_alone(self):
        rule = make_free_shipping_rule('زمان‌دار', scheduled=True)
        snapshot = get_rules()[0]
        self.assertFalse(snapshot.in_window(timezone.now()))
        self.assertTrue(snapshot.in_window(rule.starts_at + timedelta(seconds=1)))


# ======================================================================== یکپارچگی با shipping_quote
class ShippingQuoteIntegrationTests(FreeShippingBase):
    def test_courier_becomes_free_at_or_above_the_minimum_and_records_the_waived_tariff(self):
        rule = make_free_shipping_rule(min_total=2_000_000)
        rules = get_rules()
        below = self.quote(self.address(self.qom_city, self.zone), rules, 1_999_999)
        self.assertEqual((below.cost, below.label, below.free_source, below.waived_cost), (45000, shipping.LABEL_COURIER, '', 0))
        at = self.quote(self.address(self.qom_city, self.zone), rules, 2_000_000)
        self.assertEqual((at.available, at.method, at.cost), (True, 'courier', 0))
        self.assertEqual(at.label, 'ارسال رایگان (خرید بالای 2000000 تومان)')
        self.assertEqual((at.free_source, at.free_source_id, at.waived_cost), ('rule', rule.pk, 45000))

    def test_rules_are_ignored_without_a_cart_total(self):
        make_free_shipping_rule()
        quote = self.quote(self.address(self.qom_city, self.zone), get_rules(), None)
        self.assertEqual(quote.cost, 45000)

    def test_no_rules_no_change(self):
        quote = self.quote(self.address(self.qom_city, self.zone), (), 10 ** 9)
        self.assertEqual((quote.cost, quote.free_source), (45000, ''))

    def test_window_is_evaluated_with_the_given_server_time(self):
        rule = make_free_shipping_rule(scheduled=True)
        rules = get_rules()
        self.assertEqual(self.quote(self.address(self.qom_city, self.zone), rules, 1).cost, 45000)
        during = rule.starts_at + timedelta(minutes=1)
        self.assertEqual(self.quote(self.address(self.qom_city, self.zone), rules, 1, now=during).cost, 0)
        after = rule.ends_at + timedelta(minutes=1)
        self.assertEqual(self.quote(self.address(self.qom_city, self.zone), rules, 1, now=after).cost, 45000)

    def test_default_now_is_the_server_clock(self):
        rule = make_free_shipping_rule(scheduled=True)
        with mock.patch('django.utils.timezone.now', return_value=rule.starts_at + timedelta(minutes=5)):
            self.assertEqual(self.quote(self.address(self.qom_city, self.zone), get_rules(), 1).cost, 0)

    def test_province_scope(self):
        make_free_shipping_rule(min_total=500_000, provinces=[self.qom])
        rules = get_rules()
        self.assertEqual(self.quote(self.address(self.qom_city, self.zone), rules, 600_000).cost, 0)
        tehran_zone = DeliveryZone.objects.create(city=self.tehran_city, name='ناحیه‌ی تهران', shipping_cost=70000)
        self.assertEqual(self.quote(self.address(self.tehran_city, tehran_zone), rules, 600_000).cost, 70000)

    def test_city_scope_inside_a_province(self):
        make_free_shipping_rule(cities=[self.qom_city])
        rules = get_rules()
        kahak_zone = DeliveryZone.objects.create(city=self.kahak, name='ناحیه‌ی کهک', shipping_cost=60000)
        self.assertEqual(self.quote(self.address(self.qom_city, self.zone), rules, 1).cost, 0)
        self.assertEqual(self.quote(self.address(self.kahak, kahak_zone), rules, 1).cost, 60000)

    def test_unset_tariff_still_blocks_even_when_a_rule_matches(self):
        make_free_shipping_rule()
        quote = self.quote(self.address(self.qom_city, self.unpriced), get_rules(), 10 ** 9)
        self.assertEqual((quote.available, quote.reason), (False, shipping.REASON_TARIFF_UNSET))
        self.assertEqual(quote.cost, 0)

    def test_inactive_zone_and_missing_zone_still_block(self):
        make_free_shipping_rule()
        inactive = DeliveryZone.objects.create(city=self.qom_city, name='غیرفعال', shipping_cost=1000, is_active=False)
        self.assertEqual(self.quote(self.address(self.qom_city, inactive), get_rules(), 10 ** 9).reason, shipping.REASON_ZONE_INACTIVE)
        self.assertEqual(self.quote(self.address(self.qom_city, None), get_rules(), 10 ** 9).reason, shipping.REASON_ZONE_REQUIRED)
        self.assertEqual(self.quote(None, get_rules(), 10 ** 9).reason, shipping.REASON_NO_ADDRESS)

    def test_highest_priority_rule_decides_the_label(self):
        make_free_shipping_rule('کم', min_total=100, priority=1)
        make_free_shipping_rule('زیاد', min_total=200, priority=9)
        quote = self.quote(self.address(self.qom_city, self.zone), get_rules(), 1000)
        self.assertEqual(quote.label, 'ارسال رایگان (خرید بالای 200 تومان)')

    def test_a_lower_priority_rule_applies_when_the_higher_one_does_not_match(self):
        make_free_shipping_rule('زیاد', min_total=5000, priority=9)
        make_free_shipping_rule('کم', min_total=100, priority=1)
        self.assertEqual(self.quote(self.address(self.qom_city, self.zone), get_rules(), 1000).label, 'ارسال رایگان (خرید بالای 100 تومان)')

    def test_free_cart_flag_keeps_precedence_and_records_its_source(self):
        make_free_shipping_rule()
        quote = self.quote(self.address(self.qom_city, self.zone), get_rules(), 1, products=[product(True)])
        self.assertEqual((quote.label, quote.free_source, quote.waived_cost), (shipping.LABEL_COURIER_FREE, 'cart', 45000))

    # ----- پست -----
    def test_postage_cities_are_untouched_by_default(self):
        make_free_shipping_rule()
        quote = self.quote(self.address(self.shiraz), get_rules(), 10 ** 9)
        self.assertEqual((quote.method, quote.cost, quote.label, quote.free_source), ('post', 0, policy().postage_collect_label, ''))

    def test_postage_cover_mode_marks_the_shipment_free_with_the_rule_label(self):
        rule = make_free_shipping_rule(min_total=500_000, postage_mode='cover')
        rules = get_rules()
        quote = self.quote(self.address(self.shiraz), rules, 500_000)
        self.assertEqual((quote.available, quote.method, quote.cost), (True, 'post', 0))
        self.assertEqual(quote.label, 'ارسال رایگان با پست (خرید بالای 500000 تومان)')
        self.assertEqual((quote.free_source, quote.free_source_id, quote.waived_cost), ('rule', rule.pk, 0))
        below = self.quote(self.address(self.shiraz), rules, 499_999)
        self.assertEqual(below.label, policy().postage_collect_label)

    def test_postage_cover_never_overrides_a_disabled_postage_policy(self):
        make_free_shipping_rule(postage_mode='cover')
        quote = self.quote(self.address(self.shiraz), get_rules(), 10 ** 9, site=policy(postage_collect_enabled=False))
        self.assertEqual((quote.available, quote.reason), (False, shipping.REASON_POSTAGE_DISABLED))

    def test_postage_cover_respects_the_geography(self):
        make_free_shipping_rule(provinces=[self.qom], postage_mode='cover')
        self.assertEqual(self.quote(self.address(self.shiraz), get_rules(), 1).label, policy().postage_collect_label)

    # ----- کمکی بخشش (کوپن ارسال رایگان) و خلوص -----
    def test_waive_shipping_only_changes_a_courier_with_a_cost(self):
        courier = self.quote(self.address(self.qom_city, self.zone), (), 1)
        waived = waive_shipping(courier, 'ارسال رایگان با پیک (کد تخفیف X)', 'coupon', 7)
        self.assertEqual((waived.cost, waived.label, waived.free_source, waived.free_source_id, waived.waived_cost),
                         (0, 'ارسال رایگان با پیک (کد تخفیف X)', 'coupon', 7, 45000))
        self.assertEqual(courier.cost, 45000)                                   # quote اصلی تغییر نمی‌کند (frozen)
        post = self.quote(self.address(self.shiraz), (), 1)
        self.assertIs(waive_shipping(post, 'x', 'coupon', 1), post)
        blocked = self.quote(None, (), 1)
        self.assertIs(waive_shipping(blocked, 'x', 'coupon', 1), blocked)
        free = waive_shipping(waived, 'y', 'coupon', 2)
        self.assertIs(free, waived)                                             # از قبل رایگان

    def test_shipping_quote_stays_pure_and_deterministic(self):
        make_free_shipping_rule(min_total=10)
        rules = get_rules()
        address = self.address(self.qom_city, self.zone)
        now = timezone.now()
        self.assertEqual(self.quote(address, rules, 100, now=now), self.quote(address, rules, 100, now=now))

    def test_shipping_module_does_not_import_promotions(self):
        import inspect
        import re
        imports = re.findall(r'^\s*(?:from|import)\s+(\S+)', inspect.getsource(shipping), flags=re.M)
        self.assertEqual([m for m in imports if m.split('.')[0] in ('promotions', 'orders', 'cart', 'products')], [])
