"""
تست‌های مدل Address: قاعده‌ی «آدرس پیش‌فرض»، اعتبارسنجی سطح ذخیره‌سازی، حذف گروهی، ایندکس فیلترشده‌ی
SQL Server و سناریوهای هم‌زمانی (ترد واقعی روی همان دیتابیس تست).
"""

import threading

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase

from accounts.models import Address, CustomUser
from accounts.signals import default_address_changed
from locations.models import City, DeliveryZone, Province


class AddressTestMixin:
    """ استان/شهر/ناحیه‌ی آزمایشیِ مستقل از داده‌ی واقعی بارگذاری‌شده """

    def make_geo(self):
        self.province = Province.objects.create(name='استان آزمون آدرس')
        self.city = City.objects.create(province=self.province, name='شهر بدون ناحیه')
        self.zoned_city = City.objects.create(province=self.province, name='شهر ناحیه‌دار')
        self.zone = DeliveryZone.objects.create(city=self.zoned_city, name='ناحیه الف')
        self.other_zone = DeliveryZone.objects.create(city=self.city, name='ناحیه شهر دیگر', is_active=False)

    def make_user(self, phone='09120001001'):
        return CustomUser.objects.create_user(phone_number=phone)

    def make_address(self, user, **overrides):
        data = dict(user=user, title='منزل', receiver_first_name='علی', receiver_last_name='رضایی',
                    receiver_phone='09121112233', city=self.city, postal_code='1234567890', address='خیابان آزمون، پلاک ۱')
        data.update(overrides)
        return Address.objects.create(**data)

    @staticmethod
    def defaults_of(user):
        return list(Address.objects.filter(user=user, is_default=True))


class DefaultAddressRuleTests(AddressTestMixin, TestCase):
    def setUp(self):
        self.make_geo()
        self.user = self.make_user()

    def test_user_without_addresses_has_no_default(self):
        self.assertEqual(self.defaults_of(self.user), [])

    def test_first_address_becomes_default_even_if_not_requested(self):
        first = self.make_address(self.user, is_default=False)
        self.assertTrue(first.is_default)

    def test_second_address_is_not_default_unless_requested(self):
        first = self.make_address(self.user)
        second = self.make_address(self.user, title='محل کار')
        self.assertFalse(second.is_default)
        self.assertEqual(self.defaults_of(self.user), [first])

    def test_creating_with_is_default_moves_the_default(self):
        self.make_address(self.user)
        newer = self.make_address(self.user, title='جدید', is_default=True)
        self.assertEqual(self.defaults_of(self.user), [newer])

    def test_set_default_switches_and_keeps_exactly_one(self):
        first = self.make_address(self.user)
        second = self.make_address(self.user, title='محل کار')
        second.set_default()
        self.assertEqual(self.defaults_of(self.user), [second])
        first.refresh_from_db()
        first.set_default()
        self.assertEqual(self.defaults_of(self.user), [first])

    def test_cannot_unset_the_only_default_directly(self):
        first = self.make_address(self.user)
        self.make_address(self.user, title='محل کار')
        first.is_default = False
        first.save()
        first.refresh_from_db()
        self.assertTrue(first.is_default)
        self.assertEqual(len(self.defaults_of(self.user)), 1)

    def test_deleting_default_promotes_the_newest_remaining(self):
        first = self.make_address(self.user)
        second = self.make_address(self.user, title='دوم')
        third = self.make_address(self.user, title='سوم')
        first.delete()
        self.assertEqual(self.defaults_of(self.user), [third])
        third.delete()
        self.assertEqual(self.defaults_of(self.user), [second])

    def test_deleting_non_default_keeps_default_and_deleting_last_leaves_none(self):
        first = self.make_address(self.user)
        second = self.make_address(self.user, title='دوم')
        second.delete()
        self.assertEqual(self.defaults_of(self.user), [first])
        first.delete()
        self.assertEqual(self.defaults_of(self.user), [])
        self.assertEqual(Address.objects.filter(user=self.user).count(), 0)

    def test_other_users_defaults_are_independent(self):
        other = self.make_user('09120001002')
        mine = self.make_address(self.user)
        theirs = self.make_address(other)
        self.make_address(self.user, title='دوم').set_default()
        theirs.refresh_from_db()
        self.assertTrue(theirs.is_default)
        self.assertEqual(len(self.defaults_of(other)), 1)
        self.assertNotEqual(self.defaults_of(self.user), [mine])

    def test_deleting_the_user_cascades_without_error(self):
        self.make_address(self.user)
        self.make_address(self.user, title='دوم')
        self.user.delete()
        self.assertEqual(Address.objects.count(), 0)

    def test_signal_fires_after_commit_when_default_changes(self):
        received = []
        handler = lambda sender, user, address, **kw: received.append((user.pk, address.pk))
        default_address_changed.connect(handler, weak=False)
        self.addCleanup(default_address_changed.disconnect, handler)

        with self.captureOnCommitCallbacks(execute=True):
            first = self.make_address(self.user)
        with self.captureOnCommitCallbacks(execute=True):
            second = self.make_address(self.user, title='دوم')          # پیش‌فرض نشده: سیگنال نمی‌آید
        with self.captureOnCommitCallbacks(execute=True):
            second.set_default()
        second_pk = second.pk                                            # بعد از delete() مقدار pk خالی می‌شود
        with self.captureOnCommitCallbacks(execute=True):
            second.delete()                                              # جانشین (first) پیش‌فرض می‌شود

        self.assertEqual(received, [(self.user.pk, first.pk), (self.user.pk, second_pk), (self.user.pk, first.pk)])


class AddressSaveValidationTests(AddressTestMixin, TestCase):
    """ اعتبارسنجی باید حتی وقتی clean() صدا زده نشده (مثل Address.objects.create) اعمال شود """

    def setUp(self):
        self.make_geo()
        self.user = self.make_user()

    def test_zone_of_another_city_is_rejected_on_save(self):
        with self.assertRaises(ValidationError) as ctx:
            self.make_address(self.user, city=self.zoned_city, zone=self.other_zone)
        self.assertIn('zone', ctx.exception.message_dict)
        self.assertEqual(Address.objects.count(), 0)

    def test_city_with_active_zones_requires_a_zone_on_create(self):
        with self.assertRaises(ValidationError) as ctx:
            self.make_address(self.user, city=self.zoned_city)
        self.assertIn('zone', ctx.exception.message_dict)
        ok = self.make_address(self.user, city=self.zoned_city, zone=self.zone)
        self.assertEqual(ok.zone, self.zone)

    def test_city_without_zones_accepts_no_zone(self):
        self.assertIsNone(self.make_address(self.user).zone)

    def test_inactive_zone_and_inactive_city_are_rejected_on_create(self):
        self.zone.is_active = False
        self.zone.save()
        with self.assertRaises(ValidationError):
            self.make_address(self.user, city=self.zoned_city, zone=self.zone)
        self.zone.is_active = True
        self.zone.save()
        self.zoned_city.is_active = False
        self.zoned_city.save()
        with self.assertRaises(ValidationError) as ctx:
            self.make_address(self.user, city=self.zoned_city, zone=self.zone)
        self.assertIn('city', ctx.exception.message_dict)

    def test_changing_location_of_existing_address_is_validated_again(self):
        address = self.make_address(self.user)
        address.city = self.zoned_city            # ناحیه‌دار شد ولی ناحیه انتخاب نشده
        with self.assertRaises(ValidationError):
            address.save()
        address.zone = self.zone
        address.save()
        address.refresh_from_db()
        self.assertEqual((address.city, address.zone), (self.zoned_city, self.zone))

    def test_legacy_address_without_zone_can_still_be_made_default_but_clean_is_strict(self):
        """ آدرسِ منتقل‌شده از ساختار قدیمی (شهر ناحیه‌دار بدون ناحیه) با تنظیم پیش‌فرض/ویرایشِ بی‌ربط خطا نمی‌دهد،
        ولی فرمِ ویرایش (clean) انتخاب ناحیه را می‌خواهد. """
        legacy = self.make_address(self.user)
        second = self.make_address(self.user, title='دوم')
        Address.objects.filter(pk=legacy.pk).update(city=self.zoned_city)     # مثل داده‌ی انتقال‌یافته (دور زدن save)
        legacy.refresh_from_db()

        legacy.set_default()                                                    # نباید خطا بدهد
        legacy.title = 'منزل قدیمی'
        legacy.save()
        self.assertEqual(self.defaults_of(self.user), [legacy])
        with self.assertRaises(ValidationError):
            legacy.clean()
        second.refresh_from_db()

    def test_set_default_does_not_validate_content_of_incomplete_legacy_rows(self):
        first = self.make_address(self.user)
        legacy = self.make_address(self.user, title='قدیمی')
        Address.objects.filter(pk=legacy.pk).update(postal_code='', receiver_phone='')   # مثل ردیف ناقصِ منتقل‌شده
        legacy.refresh_from_db()
        legacy.set_default()                                   # نباید خطای اعتبارسنجی بدهد
        self.assertEqual(self.defaults_of(self.user), [legacy])
        with self.assertRaises(ValidationError):               # ولی ویرایش محتوا همچنان معتبر می‌خواهد
            legacy.title = 'تغییر'
            legacy.save()
        first.refresh_from_db()

    def test_phone_is_normalized_and_bad_values_rejected(self):
        address = self.make_address(self.user, receiver_phone='9121112233')
        self.assertEqual(address.receiver_phone, '09121112233')
        with self.assertRaises(ValidationError) as ctx:
            self.make_address(self.user, receiver_phone='123')
        self.assertIn('receiver_phone', ctx.exception.message_dict)

    def test_postal_code_and_required_texts(self):
        for field, value in (('postal_code', '123'), ('postal_code', 'abcdefghij'), ('title', '  '),
                             ('receiver_first_name', ''), ('receiver_last_name', ''), ('address', ' ')):
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError) as ctx:
                self.make_address(self.user, **{field: value})
            self.assertIn(field, ctx.exception.message_dict)

    def test_full_text_and_province_come_from_city(self):
        address = self.make_address(self.user, city=self.zoned_city, zone=self.zone)
        self.assertEqual(address.province, self.province)
        self.assertEqual(address.full_text, 'استان آزمون آدرس، شهر ناحیه‌دار، ناحیه الف، خیابان آزمون، پلاک ۱')
        self.assertEqual(self.make_address(self.user, title='دوم').full_text,
                         'استان آزمون آدرس، شهر بدون ناحیه، خیابان آزمون، پلاک ۱')


class BulkDeleteTests(AddressTestMixin, TestCase):
    def setUp(self):
        self.make_geo()
        self.user = self.make_user()

    def test_queryset_delete_of_default_promotes_a_survivor(self):
        a = self.make_address(self.user)
        b = self.make_address(self.user, title='ب')
        c = self.make_address(self.user, title='ج')
        Address.objects.filter(pk=a.pk).delete()
        self.assertEqual(self.defaults_of(self.user), [c])
        self.assertEqual(Address.objects.filter(user=self.user).count(), 2)

    def test_queryset_delete_of_everything_leaves_no_default_and_no_error(self):
        self.make_address(self.user)
        self.make_address(self.user, title='ب')
        Address.objects.filter(user=self.user).delete()
        self.assertEqual(Address.objects.filter(user=self.user).count(), 0)

    def test_bulk_delete_across_users_keeps_each_user_with_a_default(self):
        other = self.make_user('09120001003')
        mine = [self.make_address(self.user, title=f'م{i}') for i in range(3)]
        theirs = [self.make_address(other, title=f'ت{i}') for i in range(3)]
        doomed = [mine[0], theirs[0], theirs[1]]        # پیش‌فرض هر دو کاربر (اولین‌ها) + یک غیرپیش‌فرض
        Address.objects.filter(pk__in=[d.pk for d in doomed]).delete()
        self.assertEqual(len(self.defaults_of(self.user)), 1)
        self.assertEqual(len(self.defaults_of(other)), 1)
        self.assertEqual(Address.objects.filter(user=other).count(), 1)

    def test_admin_delete_selected_action_keeps_a_default(self):
        admin_user = CustomUser.objects.create_superuser(phone_number='09120001999')
        self.client.force_login(admin_user)
        a = self.make_address(self.user)
        b = self.make_address(self.user, title='ب')
        c = self.make_address(self.user, title='ج')

        response = self.client.post('/admin/accounts/address/', {
            'action': 'delete_selected', '_selected_action': [a.pk, c.pk], 'post': 'yes',
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(Address.objects.filter(user=self.user)), [b])
        self.assertEqual(self.defaults_of(self.user), [b])

    def test_admin_inline_and_pages_render(self):
        admin_user = CustomUser.objects.create_superuser(phone_number='09120001998')
        self.client.force_login(admin_user)
        address = self.make_address(self.user)
        for url in ('/admin/accounts/address/', '/admin/accounts/address/add/',
                    f'/admin/accounts/address/{address.pk}/change/', f'/admin/accounts/customuser/{self.user.pk}/change/'):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_admin_make_default_action(self):
        admin_user = CustomUser.objects.create_superuser(phone_number='09120001997')
        self.client.force_login(admin_user)
        a = self.make_address(self.user)
        b = self.make_address(self.user, title='ب')
        self.client.post('/admin/accounts/address/', {'action': 'make_default', '_selected_action': [b.pk]})
        self.assertEqual(self.defaults_of(self.user), [b])


class FilteredIndexTests(AddressTestMixin, TestCase):
    """ تأیید اینکه SQL Server واقعاً ایندکس *فیلترشده* را ساخته و اعمال می‌کند (نه فقط پرچم درایور) """

    def setUp(self):
        self.make_geo()

    def test_index_exists_and_is_a_filtered_unique_index(self):
        if connection.vendor != 'microsoft':
            self.skipTest('مخصوص SQL Server')
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT is_unique, has_filter, filter_definition FROM sys.indexes "
                "WHERE name = %s AND object_id = OBJECT_ID(%s)",
                ['unique_default_address_per_user', 'accounts_address'],
            )
            row = cursor.fetchone()
        self.assertIsNotNone(row, 'ایندکس ساخته نشده است')
        is_unique, has_filter, definition = row
        self.assertTrue(is_unique)
        self.assertTrue(has_filter)
        self.assertIn('is_default', definition)
        print(f'\n[filtered index] is_unique={is_unique} has_filter={has_filter} filter_definition={definition}')

    def test_database_itself_rejects_a_second_default(self):
        user = self.make_user('09120002001')
        first = self.make_address(user)
        second = self.make_address(user, title='ب')
        with self.assertRaises(IntegrityError), transaction.atomic():
            Address.objects.filter(pk=second.pk).update(is_default=True)   # دور زدن save(): دیتابیس باید جلویش را بگیرد
        first.refresh_from_db()
        self.assertTrue(first.is_default)

    def test_index_only_constrains_defaults(self):
        u1, u2 = self.make_user('09120002002'), self.make_user('09120002003')
        for user in (u1, u2):                                  # هر دو کاربر پیش‌فرض دارند (مجاز)
            self.make_address(user)
            for i in range(3):                                 # چندین ردیف غیرپیش‌فرض برای یک کاربر (مجاز)
                self.make_address(user, title=f'غیرپیش‌فرض {i}')
        self.assertEqual(Address.objects.filter(is_default=True, user__in=[u1, u2]).count(), 2)
        self.assertEqual(Address.objects.filter(is_default=False, user__in=[u1, u2]).count(), 6)


class ConcurrencyTests(AddressTestMixin, TransactionTestCase):
    """ درخواست‌های هم‌زمان واقعی (ترد + اتصال جدا برای هرکدام) نباید دو پیش‌فرض بسازند یا کاربری را بدون
    پیش‌فرض بگذارند. serialized_rollback: TransactionTestCase پایان هر تست جدول‌ها را خالی می‌کند و بدون این
    گزینه داده‌ی بذرِ استان/شهر برای تست‌های بعدی از بین می‌رفت. """

    serialized_rollback = True
    WORKERS = 6

    def setUp(self):
        self.make_geo()
        self.user = self.make_user('09120003001')

    def run_concurrently(self, jobs):
        barrier = threading.Barrier(len(jobs))
        errors = []

        def wrap(job):
            def runner():
                try:
                    barrier.wait(timeout=30)
                    job()
                except BaseException as exc:      # noqa: BLE001 - همه‌ی خطاها باید گزارش شوند
                    errors.append(repr(exc))
                finally:
                    connection.close()
            return runner

        threads = [threading.Thread(target=wrap(job)) for job in jobs]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        return errors

    def test_concurrent_first_addresses_create_exactly_one_default(self):
        jobs = [lambda i=i: self.make_address(self.user, title=f'آدرس {i}') for i in range(self.WORKERS)]
        errors = self.run_concurrently(jobs)
        self.assertEqual(errors, [])
        self.assertEqual(Address.objects.filter(user=self.user).count(), self.WORKERS)
        self.assertEqual(len(self.defaults_of(self.user)), 1)

    def test_concurrent_set_default_leaves_exactly_one(self):
        addresses = [self.make_address(self.user, title=f'آدرس {i}') for i in range(self.WORKERS)]
        errors = self.run_concurrently([lambda a=a: Address.objects.get(pk=a.pk).set_default() for a in addresses])
        self.assertEqual(errors, [])
        self.assertEqual(len(self.defaults_of(self.user)), 1)

    def test_concurrent_creates_with_default_flag_leave_exactly_one(self):
        self.make_address(self.user)
        jobs = [lambda i=i: self.make_address(self.user, title=f'پیش‌فرض {i}', is_default=True) for i in range(self.WORKERS)]
        errors = self.run_concurrently(jobs)
        self.assertEqual(errors, [])
        self.assertEqual(len(self.defaults_of(self.user)), 1)

    def test_concurrent_set_default_and_delete_never_leave_user_without_default(self):
        addresses = [self.make_address(self.user, title=f'آدرس {i}') for i in range(self.WORKERS + 2)]
        setters = [lambda a=a: Address.objects.get(pk=a.pk).set_default() for a in addresses[:3]]
        deleters = [lambda a=a: Address.objects.filter(pk=a.pk).delete() for a in addresses[3:6]]      # حذف گروهی (queryset)
        single_deleters = [lambda a=a: Address.objects.get(pk=a.pk).delete() for a in addresses[6:]]   # حذف تکی
        errors = self.run_concurrently(setters + deleters + single_deleters)
        self.assertEqual(errors, [])
        remaining = Address.objects.filter(user=self.user).count()
        self.assertEqual(remaining, 3)
        self.assertEqual(len(self.defaults_of(self.user)), 1)

    def test_concurrent_bulk_delete_of_default_and_creation(self):
        first = self.make_address(self.user)
        jobs = [lambda: Address.objects.filter(pk=first.pk).delete()] + \
               [lambda i=i: self.make_address(self.user, title=f'تازه {i}') for i in range(3)]
        errors = self.run_concurrently(jobs)
        self.assertEqual(errors, [])
        count = Address.objects.filter(user=self.user).count()
        self.assertGreaterEqual(count, 3)
        self.assertEqual(len(self.defaults_of(self.user)), 1)
