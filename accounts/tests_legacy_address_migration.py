"""
تست Data Migration آدرس‌های قدیمی (accounts.0009) و حذف ستون‌ها (0010)، شامل تمرین Rollback.

با MigrationExecutor دیتابیس تست را به نسخه‌ی قبل از انتقال (0008) برمی‌گردانیم، داده‌ی قدیمی می‌سازیم،
مایگریشن‌ها را جلو می‌بریم و نتیجه را بررسی می‌کنیم؛ در پایان همیشه به آخرین مایگریشن برمی‌گردیم.
"""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

BEFORE = [('accounts', '0008_address')]
AFTER = [('accounts', '0010_remove_legacy_address_fields')]


class LegacyAddressMigrationTests(TransactionTestCase):
    # TransactionTestCase پایان هر تست همه‌ی جدول‌ها را خالی می‌کند؛ با این گزینه داده‌ی بذرِ استان/شهر
    # برای تست‌های بعدی برمی‌گردد
    serialized_rollback = True

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return MigrationExecutor(connection).loader.project_state(targets).apps

    def setUp(self):
        self.old_apps = self.migrate(BEFORE)            # 0010 و 0009 برگردانده می‌شوند؛ ستون‌های قدیمی دوباره وجود دارند
        self.User = self.old_apps.get_model('accounts', 'CustomUser')
        self.Address = self.old_apps.get_model('accounts', 'Address')

    def tearDown(self):
        # حتی اگر تست وسط راه خراب شد، اسکیمای دیتابیس تست به آخرین مایگریشن برگردد
        loader = MigrationExecutor(connection).loader
        if ('accounts', '0010_remove_legacy_address_fields') not in loader.applied_migrations:
            # هنوز اسکیمای قدیمی است (مثلاً تست «انتقال ناموفق»): رکوردهای آزمایشیِ غیرقابل‌انتقال باید پیش از
            # جلو بردن مایگریشن‌ها پاک شوند وگرنه دوباره مایگریشن را متوقف می‌کنند
            self.User.objects.filter(phone_number__startswith='0912999').delete()
        MigrationExecutor(connection).migrate(loader.graph.leaf_nodes())
        from django.apps import apps
        apps.get_model('accounts', 'CustomUser').objects.filter(phone_number__startswith='0912999').delete()

    def make_user(self, n, **legacy):
        return self.User.objects.create(phone_number=f'0912999{n:04d}', first_name=f'نام{n}', last_name=f'فامیل{n}', **legacy)

    def test_forward_migration_creates_default_addresses_and_reports(self):
        full = self.make_user(1, state='قم', city='قم', postal_code='3749113666', address='قم پردیسان')
        arabic = self.make_user(2, state='يزد', city='يزد', postal_code='1111111111', address='خیابان تست')   # ي عربی
        new_city = self.make_user(3, state='قم', city='شهرک تازه‌ی آزمون', postal_code='2222222222', address='کوچه‌ی تست')
        empty = self.make_user(4)                                                                                 # بدون داده‌ی آدرس
        existing = self.make_user(5, state='قم', city='قم', postal_code='3333333333', address='قدیمی')
        City = self.old_apps.get_model('locations', 'City')
        self.Address.objects.create(
            user=existing, title='از قبل', receiver_first_name='الف', receiver_last_name='ب', receiver_phone='09121111111',
            city=City.objects.get(province__name='قم', name='قم'), postal_code='3333333333', address='از قبل', is_default=True)

        new_apps = self.migrate(AFTER)
        Address = new_apps.get_model('accounts', 'Address')
        User = new_apps.get_model('accounts', 'CustomUser')

        # ستون‌های قدیمی حذف شده‌اند
        self.assertFalse({'state', 'city', 'postal_code', 'address'} & {f.name for f in User._meta.get_fields()})

        a = Address.objects.get(user_id=full.pk)
        self.assertEqual((a.title, a.is_default, a.zone_id), ('آدرس اصلی', True, None))
        self.assertEqual((a.city.province.name, a.city.name), ('قم', 'قم'))
        self.assertEqual((a.postal_code, a.address), ('3749113666', 'قم پردیسان'))
        self.assertEqual((a.receiver_first_name, a.receiver_last_name, a.receiver_phone), ('نام1', 'فامیل1', '09129990001'))

        b = Address.objects.get(user_id=arabic.pk)                       # املای عربی به شهر موجود تطبیق خورد
        self.assertEqual((b.city.province.name, b.city.name), ('یزد', 'یزد'))

        c = Address.objects.get(user_id=new_city.pk)                     # شهر ناموجود زیر استانِ تطبیق‌یافته ساخته شد
        self.assertEqual((c.city.province.name, c.city.name), ('قم', 'شهرک تازه‌ی آزمون'))

        self.assertFalse(Address.objects.filter(user_id=empty.pk).exists())
        self.assertEqual(Address.objects.filter(user_id=existing.pk).count(), 1)         # ایدمپوتنت: دوباره منتقل نشد
        self.assertEqual(Address.objects.get(user_id=existing.pk).title, 'از قبل')

        for user_id in (full.pk, arabic.pk, new_city.pk, existing.pk):
            self.assertEqual(Address.objects.filter(user_id=user_id, is_default=True).count(), 1)

    def test_untransferable_address_stops_the_migration_before_columns_are_dropped(self):
        ok = self.make_user(10, state='قم', city='قم', postal_code='3749113666', address='ok')
        bad = self.make_user(11, state='استان کاملاً ناشناخته', city='شهر', postal_code='1', address='گم می‌شد')

        with self.assertRaises(RuntimeError) as ctx:
            self.migrate(AFTER)
        self.assertIn('1 کاربر', str(ctx.exception))

        # هیچ‌چیز اعمال نشد: ستون‌های قدیمی و داده‌شان سرجایشان هستند و آدرسی ساخته نشده
        self.assertTrue(MigrationExecutor(connection).loader.applied_migrations.keys() >= {BEFORE[0]})
        self.assertNotIn(('accounts', '0009_migrate_legacy_addresses'), MigrationExecutor(connection).loader.applied_migrations)
        self.assertEqual(self.User.objects.get(pk=bad.pk).address, 'گم می‌شد')
        self.assertFalse(self.Address.objects.filter(user_id__in=[ok.pk, bad.pk]).exists())

    def test_rollback_restores_legacy_columns_from_the_default_address(self):
        user = self.make_user(20, state='قم', city='قم', postal_code='3749113666', address='قم پردیسان')
        self.migrate(AFTER)

        old_apps = self.migrate(BEFORE)                                   # Rollback به قبل از انتقال
        User = old_apps.get_model('accounts', 'CustomUser')
        restored = User.objects.get(pk=user.pk)
        self.assertEqual((restored.state, restored.city, restored.postal_code, restored.address),
                         ('قم', 'قم', '3749113666', 'قم پردیسان'))

        # و دوباره جلو رفتن هم سالم است (آدرس تکراری ساخته نمی‌شود چون از قبل آدرس دارد)
        new_apps = self.migrate(AFTER)
        self.assertEqual(new_apps.get_model('accounts', 'Address').objects.filter(user_id=user.pk).count(), 1)
