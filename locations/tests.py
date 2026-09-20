from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import TestCase

from . import seed
from .models import City, DeliveryZone, Province
from .text import normalize_fa


class NormalizeFaTests(TestCase):
    def test_arabic_letters_zwnj_and_spaces_are_unified(self):
        self.assertEqual(normalize_fa('  اصفهان  '), 'اصفهان')
        self.assertEqual(normalize_fa('كرمان'), 'کرمان')          # ك عربی
        self.assertEqual(normalize_fa('يزد'), 'یزد')              # ي عربی
        self.assertEqual(normalize_fa('چهارمحال‌و  بختیاری'), 'چهارمحال و بختیاری')

    def test_empty_values(self):
        self.assertEqual(normalize_fa(None), '')
        self.assertEqual(normalize_fa('   '), '')


class LoaderTests(TestCase):
    DATA = {'provinces': [
        {'name': 'استان آزمایشی', 'cities': ['شهر یک', 'شهر دو', '  ', 'شهر یک']},
        {'name': '', 'cities': ['نباید ساخته شود']},
    ]}

    def test_creates_provinces_and_cities_and_skips_blanks_and_duplicates(self):
        stats = seed.load_divisions(self.DATA, Province, City)
        province = Province.objects.get(name='استان آزمایشی')
        self.assertEqual(sorted(province.cities.values_list('name', flat=True)), ['شهر دو', 'شهر یک'])
        self.assertEqual(stats['provinces_created'], 1)
        self.assertEqual(stats['cities_created'], 2)
        self.assertFalse(City.objects.filter(name='نباید ساخته شود').exists())

    def test_reload_is_idempotent_and_keeps_admin_edits(self):
        seed.load_divisions(self.DATA, Province, City)
        city = City.objects.get(name='شهر یک')
        city.is_active = False
        city.save()

        stats = seed.load_divisions(self.DATA, Province, City)

        self.assertEqual(stats['provinces_created'], 0)
        self.assertEqual(stats['cities_created'], 0)
        self.assertEqual(Province.objects.filter(name='استان آزمایشی').count(), 1)
        self.assertEqual(City.objects.filter(name='شهر یک').count(), 1)
        city.refresh_from_db()
        self.assertFalse(city.is_active)

    def test_arabic_spelling_matches_existing_record(self):
        seed.load_divisions({'provinces': [{'name': 'استان کرمان‌آزمون', 'cities': ['شهر کرمان‌آزمون']}]}, Province, City)
        seed.load_divisions({'provinces': [{'name': 'استان كرمان‌آزمون', 'cities': ['شهر كرمان‌آزمون']}]}, Province, City)
        self.assertEqual(Province.objects.filter(name__contains='آزمون').count(), 1)
        self.assertEqual(City.objects.filter(name__contains='آزمون').count(), 1)


class InitialZonesTests(TestCase):
    ZONES = {('استان آزمایشی', 'شهر ناحیه‌دار'): ('ناحیه الف', 'ناحیه ب')}

    def setUp(self):
        province = Province.objects.create(name='استان آزمایشی')
        self.city = City.objects.create(province=province, name='شهر ناحیه‌دار')

    def test_zones_created_with_unset_tariff(self):
        created = seed.seed_initial_zones(Province, City, DeliveryZone, zones=self.ZONES)
        self.assertEqual(created, 2)
        zones = DeliveryZone.objects.filter(city=self.city)
        self.assertEqual(zones.count(), 2)
        for zone in zones:
            self.assertEqual(zone.shipping_cost, 0)
            self.assertFalse(zone.has_tariff)   # ۰ = «تعرفه تنظیم‌نشده»

    def test_reseeding_does_not_duplicate_or_reset_tariff(self):
        seed.seed_initial_zones(Province, City, DeliveryZone, zones=self.ZONES)
        zone = DeliveryZone.objects.get(city=self.city, name='ناحیه الف')
        zone.shipping_cost = 45000
        zone.save()

        self.assertEqual(seed.seed_initial_zones(Province, City, DeliveryZone, zones=self.ZONES), 0)
        zone.refresh_from_db()
        self.assertEqual(zone.shipping_cost, 45000)
        self.assertTrue(zone.has_tariff)

    def test_missing_city_is_skipped_silently(self):
        zones = {('استان نامعلوم', 'شهر نامعلوم'): ('الف',)}
        self.assertEqual(seed.seed_initial_zones(Province, City, DeliveryZone, zones=zones), 0)


class ModelRulesTests(TestCase):
    def setUp(self):
        self.province = Province.objects.create(name='استان آزمایشی')
        self.city = City.objects.create(province=self.province, name='شهر آزمایشی')

    def test_city_name_is_unique_per_province_only(self):
        other = Province.objects.create(name='استان دیگر')
        City.objects.create(province=other, name='شهر آزمایشی')   # در استان دیگر مجاز است
        with self.assertRaises(IntegrityError), transaction.atomic():
            City.objects.create(province=self.province, name='شهر آزمایشی')

    def test_zone_name_is_unique_per_city(self):
        DeliveryZone.objects.create(city=self.city, name='الف')
        with self.assertRaises(IntegrityError), transaction.atomic():
            DeliveryZone.objects.create(city=self.city, name='الف')

    def test_city_has_zones_only_with_an_active_zone(self):
        self.assertFalse(self.city.has_zones)
        zone = DeliveryZone.objects.create(city=self.city, name='الف', is_active=False)
        self.assertFalse(self.city.has_zones)
        zone.is_active = True
        zone.save()
        self.assertTrue(self.city.has_zones)

    def test_provinces_and_cities_cannot_be_deleted_while_referenced(self):
        with self.assertRaises(ProtectedError):
            self.province.delete()
        DeliveryZone.objects.create(city=self.city, name='الف')
        with self.assertRaises(ProtectedError):
            self.city.delete()


class AdminSmokeTests(TestCase):
    """ صفحه‌های ادمین (لیست، افزودن، ویرایش با اینلاین نواحی) بدون خطا رندر می‌شوند """

    def setUp(self):
        from accounts.models import CustomUser
        self.admin = CustomUser.objects.create_superuser(phone_number='09120000901')
        self.client.force_login(self.admin)
        self.province = Province.objects.create(name='استان آزمایشی')
        self.city = City.objects.create(province=self.province, name='شهر آزمایشی')
        self.zone = DeliveryZone.objects.create(city=self.city, name='ناحیه الف')

    def test_changelist_add_and_change_pages_render(self):
        for model, obj in ((Province, self.province), (City, self.city), (DeliveryZone, self.zone)):
            name = model._meta.model_name
            for view, args in (('changelist', ()), ('add', ()), ('change', (obj.pk,))):
                with self.subTest(model=name, view=view):
                    response = self.client.get(f'/admin/locations/{name}/' + {
                        'changelist': '', 'add': 'add/', 'change': f'{obj.pk}/change/'}[view])
                    self.assertEqual(response.status_code, 200)

    def test_zone_shows_unset_tariff_label(self):
        response = self.client.get('/admin/locations/deliveryzone/')
        self.assertContains(response, 'تعرفه تنظیم‌نشده')


class SeededDataTests(TestCase):
    """ داده‌ی واقعی که Data Migration بارگذاری کرده (استان/شهر + نواحی قم) """

    def test_provinces_and_cities_loaded(self):
        self.assertEqual(Province.objects.filter(cities__isnull=False).distinct().count() >= 31, True)
        self.assertGreaterEqual(City.objects.count(), 1449)

    def test_numbered_municipal_districts_are_not_cities(self):
        import re
        numbered = [n for n in City.objects.values_list('name', flat=True) if re.search(r'[\d۰-۹]', n)]
        self.assertEqual(numbered, [])

    def test_cities_that_only_existed_as_numbered_records_are_kept(self):
        self.assertTrue(City.objects.filter(province__name='تهران', name='اسلام شهر').exists())
        self.assertTrue(City.objects.filter(province__name='البرز', name='کرج').exists())

    def test_qom_cities_are_active_and_have_no_zones_except_qom_itself(self):
        qom = Province.objects.get(name='قم')
        names = set(qom.cities.values_list('name', flat=True))
        self.assertTrue({'قم', 'کهک', 'جعفریه', 'قنوات', 'سلفچگان', 'دستجرد'} <= names)
        self.assertFalse(qom.cities.filter(is_active=False).exists())
        self.assertTrue(City.objects.get(province=qom, name='قم').has_zones)
        self.assertFalse(City.objects.get(province=qom, name='کهک').has_zones)

    def test_qom_initial_zones_exist_with_unset_tariff(self):
        zones = DeliveryZone.objects.filter(city__province__name='قم', city__name='قم')
        self.assertEqual(
            set(zones.values_list('name', flat=True)),
            {'نیروگاه', 'پردیسان', 'کهک', 'سالاریه / زنبیل‌آباد', 'بنیاد / صفاشهر', 'باجک / عماریاسر', 'سایر نواحی قم'},
        )
        self.assertTrue(all(z.is_active and not z.has_tariff for z in zones))
