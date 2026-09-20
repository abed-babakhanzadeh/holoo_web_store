"""
تست صفحه‌های مدیریت آدرس پنل (لیست/افزودن/ویرایش/حذف/پیش‌فرض) و قطعه‌های htmx کمبوهای وابسته.
"""

from django.test import TestCase
from django.urls import reverse

from accounts.models import Address, CustomUser
from locations.models import City, DeliveryZone, Province


class AddressViewsTestBase(TestCase):
    def setUp(self):
        self.province = Province.objects.create(name='استان صفحه‌ی آدرس')
        self.other_province = Province.objects.create(name='استان دیگر صفحه')
        self.city = City.objects.create(province=self.province, name='شهر پستی')                 # بدون ناحیه
        self.zoned_city = City.objects.create(province=self.province, name='شهر پیکی')           # ناحیه‌دار
        self.zone = DeliveryZone.objects.create(city=self.zoned_city, name='ناحیه الف')
        self.zone_b = DeliveryZone.objects.create(city=self.zoned_city, name='ناحیه ب')
        self.inactive_zone = DeliveryZone.objects.create(city=self.zoned_city, name='ناحیه غیرفعال', is_active=False)
        self.inactive_city = City.objects.create(province=self.province, name='شهر غیرفعال', is_active=False)
        self.foreign_city = City.objects.create(province=self.other_province, name='شهر استان دیگر')

        self.user = CustomUser.objects.create_user(phone_number='09120004001', first_name='علی', last_name='رضایی')
        self.other = CustomUser.objects.create_user(phone_number='09120004002')
        self.client.force_login(self.user)

    def payload(self, **overrides):
        data = {'title': 'منزل', 'province': self.province.pk, 'city': self.city.pk, 'address': 'خیابان تست، پلاک ۱',
                'postal_code': '1234567890', 'receiver_first_name': 'علی', 'receiver_last_name': 'رضایی',
                'receiver_phone': '09121112233'}
        data.update(overrides)
        return data

    def make(self, user=None, **overrides):
        data = dict(user=user or self.user, title='منزل', receiver_first_name='علی', receiver_last_name='رضایی',
                    receiver_phone='09121112233', city=self.city, postal_code='1234567890', address='خیابان تست')
        data.update(overrides)
        return Address.objects.create(**data)


class AccessAndListTests(AddressViewsTestBase):
    def test_all_pages_require_login(self):
        self.client.logout()
        address = self.make()
        for name, args, method in (('address_list', (), 'get'), ('address_create', (), 'get'),
                                   ('address_edit', (address.pk,), 'get'), ('address_delete', (address.pk,), 'post'),
                                   ('address_set_default', (address.pk,), 'post')):
            with self.subTest(name=name):
                response = getattr(self.client, method)(reverse(f'accounts:{name}', args=args))
                self.assertEqual(response.status_code, 302)
                self.assertIn('login', response.url)

    def test_list_shows_only_own_addresses_with_default_badge(self):
        mine = self.make(title='منزل من')
        second = self.make(title='محل کار من')
        self.make(user=self.other, title='عنوانِ-متعلق-به-کاربر-دیگر')
        html = self.client.get(reverse('accounts:address_list')).content.decode()
        self.assertIn('منزل من', html)
        self.assertIn('محل کار من', html)
        self.assertNotIn('عنوانِ-متعلق-به-کاربر-دیگر', html)
        self.assertEqual(html.count('پیش‌فرض</span>'), 1)                    # فقط نشان روی کارت پیش‌فرض
        self.assertIn(reverse('accounts:address_set_default', args=[second.pk]), html)
        self.assertNotIn(reverse('accounts:address_set_default', args=[mine.pk]), html)   # پیش‌فرض دکمه‌ی «تنظیم پیش‌فرض» ندارد

    def test_empty_state(self):
        response = self.client.get(reverse('accounts:address_list'))
        self.assertContains(response, 'هنوز آدرسی ثبت نکرده‌اید')
        self.assertContains(response, reverse('accounts:address_create'))

    def test_address_in_zoned_city_without_zone_shows_completion_warning(self):
        legacy = self.make()
        Address.objects.filter(pk=legacy.pk).update(city=self.zoned_city)     # مثل آدرسِ منتقل‌شده‌ی قدیمی
        response = self.client.get(reverse('accounts:address_list'))
        self.assertContains(response, 'باید ناحیه را مشخص کنید')
        self.assertContains(response, reverse('accounts:address_edit', args=[legacy.pk]))

    def test_dashboard_navigation_links_to_addresses(self):
        self.assertContains(self.client.get(reverse('accounts:dashboard')), reverse('accounts:address_list'))


class CreateTests(AddressViewsTestBase):
    def test_form_prefills_receiver_from_profile_and_locks_default_for_first_address(self):
        html = self.client.get(reverse('accounts:address_create')).content.decode()
        self.assertIn('value="علی"', html)
        self.assertIn('value="رضایی"', html)
        self.assertIn('value="09120004001"', html)
        self.assertIn('اولین آدرس شما به‌صورت خودکار پیش‌فرض می‌شود', html)

    def test_first_address_becomes_default_and_redirects_with_message(self):
        response = self.client.post(reverse('accounts:address_create'), self.payload(), follow=True)
        self.assertRedirects(response, reverse('accounts:address_list'))
        address = Address.objects.get(user=self.user)
        self.assertTrue(address.is_default)
        self.assertContains(response, 'آدرس جدید ثبت شد.')

    def test_second_address_is_not_default_unless_checked(self):
        self.make()
        self.client.post(reverse('accounts:address_create'), self.payload(title='محل کار'))
        second = Address.objects.get(title='محل کار')
        self.assertFalse(second.is_default)
        self.client.post(reverse('accounts:address_create'), self.payload(title='سوم', is_default='on'))
        self.assertEqual(list(Address.objects.filter(user=self.user, is_default=True).values_list('title', flat=True)), ['سوم'])

    def test_no_limit_on_number_of_addresses(self):
        for i in range(15):
            self.client.post(reverse('accounts:address_create'), self.payload(title=f'آدرس {i}'))
        self.assertEqual(Address.objects.filter(user=self.user).count(), 15)

    def test_zoned_city_requires_a_zone(self):
        response = self.client.post(reverse('accounts:address_create'), self.payload(city=self.zoned_city.pk))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'برای این شهر انتخاب ناحیه الزامی است')
        self.assertFalse(Address.objects.exists())
        # ناحیه‌ی درست: ثبت می‌شود
        self.client.post(reverse('accounts:address_create'), self.payload(city=self.zoned_city.pk, zone=self.zone.pk))
        self.assertEqual(Address.objects.get().zone, self.zone)

    def test_zone_form_state_is_preserved_after_error(self):
        response = self.client.post(reverse('accounts:address_create'),
                                    self.payload(city=self.zoned_city.pk, postal_code='12'))
        html = response.content.decode()
        self.assertContains(response, 'کد پستی باید ۱۰ رقم عددی باشد')
        self.assertIn('id="id_zone"', html)                                       # کمبوی ناحیه برای شهر ناحیه‌دار دوباره رندر شد
        self.assertIn(f'value="{self.zoned_city.pk}" selected', html)             # شهر انتخاب‌شده حفظ شد
        self.assertIn(f'value="{self.province.pk}" selected', html)               # استان انتخاب‌شده حفظ شد

    def test_zone_of_other_city_inactive_zone_and_mismatched_province_are_rejected(self):
        cases = (
            dict(city=self.zoned_city.pk, zone=DeliveryZone.objects.create(city=self.city, name='ناحیه‌ی شهر دیگر').pk),
            dict(city=self.zoned_city.pk, zone=self.inactive_zone.pk),
            dict(city=self.foreign_city.pk),                                       # شهرِ استانی غیر از استان انتخاب‌شده
            dict(city=self.inactive_city.pk),                                      # شهر غیرفعال
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                response = self.client.post(reverse('accounts:address_create'), self.payload(**overrides))
                self.assertEqual(response.status_code, 200)
        self.assertFalse(Address.objects.exists())

    def test_invalid_input_is_rejected_with_messages(self):
        response = self.client.post(reverse('accounts:address_create'), self.payload(
            title='', receiver_phone='123', address='  ', receiver_first_name=''))
        html = response.content.decode()
        for expected in ('عنوان آدرس الزامی است', 'نام گیرنده الزامی است', 'آدرس دقیق الزامی است'):
            with self.subTest(expected=expected):
                self.assertIn(expected, html)
        self.assertFalse(Address.objects.exists())

    def test_user_cannot_create_address_for_someone_else(self):
        self.client.post(reverse('accounts:address_create'), self.payload(user=self.other.pk))
        self.assertEqual(Address.objects.get().user, self.user)


class EditTests(AddressViewsTestBase):
    def test_edit_form_is_prefilled(self):
        address = self.make(city=self.zoned_city, zone=self.zone, title='قابل ویرایش')
        html = self.client.get(reverse('accounts:address_edit', args=[address.pk])).content.decode()
        self.assertIn('value="قابل ویرایش"', html)
        self.assertIn(f'value="{self.province.pk}" selected', html)
        self.assertIn(f'value="{self.zoned_city.pk}" selected', html)
        self.assertIn(f'value="{self.zone.pk}" selected', html)
        self.assertIn('این آدرس پیش‌فرض فعلی است', html)

    def test_edit_saves_changes(self):
        address = self.make()
        response = self.client.post(reverse('accounts:address_edit', args=[address.pk]),
                                    self.payload(title='عنوان جدید', city=self.zoned_city.pk, zone=self.zone_b.pk))
        self.assertRedirects(response, reverse('accounts:address_list'))
        address.refresh_from_db()
        self.assertEqual((address.title, address.city, address.zone), ('عنوان جدید', self.zoned_city, self.zone_b))

    def test_other_users_address_is_404_for_all_actions(self):
        foreign = self.make(user=self.other)
        for name, method in (('address_edit', 'get'), ('address_edit', 'post'),
                             ('address_delete', 'post'), ('address_set_default', 'post')):
            with self.subTest(name=name, method=method):
                response = getattr(self.client, method)(reverse(f'accounts:{name}', args=[foreign.pk]))
                self.assertEqual(response.status_code, 404)
        foreign.refresh_from_db()
        self.assertEqual(foreign.title, 'منزل')

    def test_legacy_address_without_zone_opens_cleanly_and_forces_zone_choice(self):
        legacy = self.make()
        Address.objects.filter(pk=legacy.pk).update(city=self.zoned_city)       # شهرِ ناحیه‌دار بدون ناحیه
        url = reverse('accounts:address_edit', args=[legacy.pk])

        response = self.client.get(url)                                          # فرم بدون خطا باز می‌شود
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertContains(response, 'باید <b>ناحیه</b> مشخص شود')
        self.assertIn('id="id_zone"', html)
        self.assertNotIn(f'value="{self.zone.pk}" selected', html)               # هیچ ناحیه‌ای از پیش انتخاب نشده
        self.assertIn('value="">انتخاب ناحیه', html)

        payload = self.payload(city=self.zoned_city.pk)                          # بدون ناحیه: ذخیره نمی‌شود
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'برای این شهر انتخاب ناحیه الزامی است')
        legacy.refresh_from_db()
        self.assertIsNone(legacy.zone)

        response = self.client.post(url, {**payload, 'zone': self.zone.pk})      # با ناحیه: ذخیره می‌شود
        self.assertRedirects(response, reverse('accounts:address_list'))
        legacy.refresh_from_db()
        self.assertEqual(legacy.zone, self.zone)

    def test_editing_default_address_cannot_unset_default(self):
        first = self.make()
        self.make(title='دوم')
        self.client.post(reverse('accounts:address_edit', args=[first.pk]), self.payload())      # بدون is_default در فرم
        first.refresh_from_db()
        self.assertTrue(first.is_default)

    def test_checkbox_default_on_edit_of_non_default_moves_default(self):
        self.make()
        second = self.make(title='دوم')
        self.client.post(reverse('accounts:address_edit', args=[second.pk]), self.payload(title='دوم', is_default='on'))
        second.refresh_from_db()
        self.assertTrue(second.is_default)
        self.assertEqual(Address.objects.filter(user=self.user, is_default=True).count(), 1)


class DeleteAndDefaultTests(AddressViewsTestBase):
    def test_delete_requires_post(self):
        address = self.make()
        self.assertEqual(self.client.get(reverse('accounts:address_delete', args=[address.pk])).status_code, 405)
        self.assertEqual(self.client.get(reverse('accounts:address_set_default', args=[address.pk])).status_code, 405)
        self.assertTrue(Address.objects.filter(pk=address.pk).exists())

    def test_delete_default_promotes_another_and_tells_the_user(self):
        first = self.make(title='اول')
        second = self.make(title='دوم')
        response = self.client.post(reverse('accounts:address_delete', args=[first.pk]), follow=True)
        self.assertContains(response, 'آدرس «اول» حذف شد.')
        self.assertContains(response, 'آدرس «دوم» به‌عنوان آدرس پیش‌فرض تنظیم شد.')
        second.refresh_from_db()
        self.assertTrue(second.is_default)

    def test_delete_last_address_leaves_no_default(self):
        only = self.make()
        self.client.post(reverse('accounts:address_delete', args=[only.pk]))
        self.assertFalse(Address.objects.filter(user=self.user).exists())

    def test_set_default_switches(self):
        self.make()
        second = self.make(title='دوم')
        response = self.client.post(reverse('accounts:address_set_default', args=[second.pk]), follow=True)
        self.assertContains(response, 'آدرس پیش‌فرض شد')
        self.assertEqual(list(Address.objects.filter(user=self.user, is_default=True)), [second])


class CascadeEndpointTests(AddressViewsTestBase):
    def test_city_options_lists_only_active_cities_of_the_province_and_resets_zone(self):
        html = self.client.get(reverse('locations:city_options'), {'province': self.province.pk}).content.decode()
        self.assertIn('شهر پستی', html)
        self.assertIn('شهر پیکی', html)
        self.assertNotIn('شهر غیرفعال', html)
        self.assertNotIn('شهر استان دیگر', html)
        self.assertIn('انتخاب شهر', html)
        self.assertIn('id="zone-field" hx-swap-oob="true"', html)

    def test_city_options_with_bad_or_missing_province_only_has_placeholder(self):
        for params in ({}, {'province': 'abc'}, {'province': '999999'}):
            with self.subTest(params=params):
                html = self.client.get(reverse('locations:city_options'), params).content.decode()
                self.assertIn('انتخاب شهر', html)
                self.assertNotIn('<option value="1"', html)

    def test_zone_field_for_zoned_city_lists_only_active_zones(self):
        html = self.client.get(reverse('locations:zone_field'), {'city': self.zoned_city.pk}).content.decode()
        self.assertIn('id="zone-field"', html)
        self.assertIn('ناحیه الف', html)
        self.assertIn('ناحیه ب', html)
        self.assertNotIn('ناحیه غیرفعال', html)
        self.assertIn('required', html)

    def test_zone_field_for_city_without_zones_is_an_empty_container(self):
        for city in (self.city, self.foreign_city):
            with self.subTest(city=city.name):
                html = self.client.get(reverse('locations:zone_field'), {'city': city.pk}).content.decode()
                self.assertIn('id="zone-field"', html)
                self.assertNotIn('<select', html)
        html = self.client.get(reverse('locations:zone_field'), {'city': 'xyz'}).content.decode()
        self.assertNotIn('<select', html)

    def test_endpoints_require_login_and_return_401_not_a_login_page(self):
        self.client.logout()
        for name in ('locations:city_options', 'locations:zone_field'):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 401)

    def test_endpoints_are_get_only(self):
        for name in ('locations:city_options', 'locations:zone_field'):
            with self.subTest(name=name):
                self.assertEqual(self.client.post(reverse(name)).status_code, 405)
