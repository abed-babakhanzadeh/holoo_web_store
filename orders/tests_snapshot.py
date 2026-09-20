"""
تست اسنپ‌شات مقصد/ارسال در Order: full_address، order_snapshot، استقلال از تغییرات بعدی آدرس/تعرفه،
ذخیره‌ی درستِ حروف فارسی (بدون تبدیل به «ي/ك» عربی) و مایگریشن روی سفارش‌های موجود.
"""

from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase

from accounts.models import Address, CustomUser
from locations.models import City, DeliveryZone, Province
from orders.models import Order
from orders.shipping import shipping_quote
from orders.snapshot import order_snapshot


def policy(**overrides):
    data = dict(courier_free_for_free_shipping_cart=True, postage_collect_enabled=True,
                postage_collect_label='پس‌کرایه (پرداخت هزینه درب منزل)',
                postage_disabled_message='امکان ارسال به این شهر فعلاً وجود ندارد.')
    data.update(overrides)
    return SimpleNamespace(**data)


class SnapshotBase(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(phone_number='09120008001', first_name='علی', last_name='رضایی')
        self.province = Province.objects.create(name='استان یزد‌آزمون')
        self.zoned_city = City.objects.create(province=self.province, name='کرمانِ آزمون')
        self.zone = DeliveryZone.objects.create(city=self.zoned_city, name='ناحیه‌ی پیکیِ آزمون', shipping_cost=45000)
        self.post_city = City.objects.create(province=self.province, name='شهر پستیِ آزمون')

    def address(self, city, zone=None, **overrides):
        data = dict(user=self.user, title='منزل', receiver_first_name='مریم', receiver_last_name='کاظمی',
                    receiver_phone='09123334455', city=city, zone=zone, postal_code='1112223334',
                    address='بلوار آزمون، پلاک ۵')
        data.update(overrides)
        return Address.objects.create(**data)

    def place_order(self, address, products=(SimpleNamespace(free_shipping=False),), **quote_overrides):
        quote = shipping_quote(address, list(products), policy(**quote_overrides))
        return Order.objects.create(user=self.user, payment_method='cash', total_price=100000 + quote.cost,
                                    **order_snapshot(address, quote))


class FullAddressTests(TestCase):
    def test_all_parts_joined_in_order(self):
        order = Order(province='قم', city='قم', zone='پردیسان', address='بلوار پردیسان، فاز ۲')
        self.assertEqual(order.full_address, 'قم، قم، پردیسان، بلوار پردیسان، فاز ۲')

    def test_without_zone(self):
        order = Order(province='تهران', city='تهران', zone='', address='خیابان ولیعصر')
        self.assertEqual(order.full_address, 'تهران، تهران، خیابان ولیعصر')

    def test_legacy_order_returns_the_stored_address_unchanged(self):
        order = Order(address='تهران، خیابان آزادی، پلاک ۱')
        self.assertEqual(order.full_address, 'تهران، خیابان آزادی، پلاک ۱')

    def test_blank_and_whitespace_parts_are_skipped(self):
        order = Order(province=' قم ', city='  ', zone='', address=' خیابان ')
        self.assertEqual(order.full_address, 'قم، خیابان')


class OrderSnapshotTests(SnapshotBase):
    def test_courier_snapshot_copies_receiver_destination_and_shipping(self):
        address = self.address(self.zoned_city, self.zone)
        quote = shipping_quote(address, [SimpleNamespace(free_shipping=False)], policy())
        self.assertEqual(order_snapshot(address, quote), {
            'first_name': 'مریم', 'last_name': 'کاظمی', 'phone': '09123334455',
            'address': 'بلوار آزمون، پلاک ۵', 'postal_code': '1112223334',
            'province': 'استان یزد‌آزمون', 'city': 'کرمانِ آزمون', 'zone': 'ناحیه‌ی پیکیِ آزمون',
            'shipping_method': 'courier', 'shipping_label': 'ارسال با پیک', 'shipping_cost': 45000,
        })

    def test_postage_snapshot_has_no_zone_zero_cost_and_the_label(self):
        address = self.address(self.post_city)
        quote = shipping_quote(address, [SimpleNamespace(free_shipping=False)], policy(postage_collect_label='کرایه با گیرنده'))
        snap = order_snapshot(address, quote)
        self.assertEqual((snap['zone'], snap['shipping_method'], snap['shipping_cost'], snap['shipping_label']),
                         ('', 'post', 0, 'کرایه با گیرنده'))

    def test_blocked_quote_can_never_be_snapshotted(self):
        address = self.address(self.post_city)
        quote = shipping_quote(address, [SimpleNamespace(free_shipping=False)], policy(postage_collect_enabled=False))
        with self.assertRaises(ValueError):
            order_snapshot(address, quote)

    def test_order_created_from_snapshot_reads_back_correctly(self):
        order = Order.objects.get(pk=self.place_order(self.address(self.zoned_city, self.zone)).pk)
        self.assertEqual((order.province, order.city, order.zone), ('استان یزد‌آزمون', 'کرمانِ آزمون', 'ناحیه‌ی پیکیِ آزمون'))
        self.assertEqual((order.shipping_method, order.shipping_label, int(order.shipping_cost)), ('courier', 'ارسال با پیک', 45000))
        self.assertEqual(order.full_address, 'استان یزد‌آزمون، کرمانِ آزمون، ناحیه‌ی پیکیِ آزمون، بلوار آزمون، پلاک ۵')
        self.assertEqual((order.first_name, order.last_name, order.phone), ('مریم', 'کاظمی', '09123334455'))


class SnapshotIndependenceTests(SnapshotBase):
    """ تغییر/حذف آدرس، تغییر نام و تعرفه، هیچ‌کدام فاکتورِ ثبت‌شده را عوض نمی‌کنند """

    def snapshot_of(self, order):
        order = Order.objects.get(pk=order.pk)
        return (order.first_name, order.last_name, order.phone, order.address, order.postal_code, order.province,
                order.city, order.zone, order.shipping_method, order.shipping_label, int(order.shipping_cost), order.full_address)

    def test_later_changes_to_address_zone_city_and_tariff_do_not_touch_the_order(self):
        address = self.address(self.zoned_city, self.zone)
        order = self.place_order(address)
        before = self.snapshot_of(order)

        Address.objects.filter(pk=address.pk).update(receiver_first_name='کس دیگر', address='جای دیگر', postal_code='9999999999')
        DeliveryZone.objects.filter(pk=self.zone.pk).update(name='نام جدید ناحیه', shipping_cost=99000)
        City.objects.filter(pk=self.zoned_city.pk).update(name='نام جدید شهر')
        Province.objects.filter(pk=self.province.pk).update(name='نام جدید استان')

        self.assertEqual(self.snapshot_of(order), before)

    def test_deleting_the_address_or_the_user_keeps_the_invoice_intact(self):
        address = self.address(self.zoned_city, self.zone)
        order = self.place_order(address)
        before = self.snapshot_of(order)

        address.delete()
        self.assertEqual(self.snapshot_of(order), before)
        self.user.delete()                                   # user → SET_NULL؛ خود فاکتور می‌ماند
        self.assertEqual(self.snapshot_of(order), before)
        self.assertTrue(Order.objects.filter(pk=order.pk).exists())

    def test_order_has_no_foreign_key_to_locations_or_addresses(self):
        related = {f.related_model for f in Order._meta.get_fields() if f.is_relation and f.concrete}
        self.assertFalse({Address, City, Province, DeliveryZone} & related)


class PersianTextStorageTests(SnapshotBase):
    """ حروف فارسی «ی/ک» نباید هنگام ذخیره/خواندن به «ي/ك» عربی تبدیل شوند (باگ کدپیج SQL Server در DDL) """

    def test_snapshot_text_round_trips_with_persian_letters(self):
        order = self.place_order(self.address(self.post_city), postage_collect_label='پس‌کرایه (پرداخت هزینه درب منزل)')
        order = Order.objects.get(pk=order.pk)
        for value in (order.province, order.city, order.zone, order.shipping_label, order.address, order.full_address):
            self.assertNotRegex(value, '[يك]')
        self.assertIn('ی', order.province)                       # «یزد» با ی فارسی
        self.assertEqual(order.shipping_label, 'پس‌کرایه (پرداخت هزینه درب منزل)')

    def test_field_defaults_are_empty_strings(self):
        order = Order.objects.create(user=self.user, first_name='الف', last_name='ب', phone='09121112233',
                                     address='آدرس', payment_method='cash', total_price=1)
        order = Order.objects.get(pk=order.pk)
        self.assertEqual((order.province, order.city, order.zone, order.shipping_method, order.shipping_label), ('',) * 5)

    def test_shipping_method_only_accepts_known_values(self):
        order = Order(user=self.user, first_name='الف', last_name='ب', phone='09121112233', address='آدرس',
                      payment_method='cash', total_price=1, shipping_method='drone')
        with self.assertRaises(ValidationError) as ctx:
            order.full_clean()
        self.assertIn('shipping_method', ctx.exception.message_dict)
        for ok in ('', 'courier', 'post'):
            order.shipping_method = ok
            order.full_clean()


class ExistingOrdersMigrationTests(TransactionTestCase):
    """ مایگریشن 0009 روی سفارش‌های موجود: داده‌ی قبلی دست‌نخورده، فیلدهای جدید خالی """

    serialized_rollback = True

    def tearDown(self):
        MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())

    def _migrate(self, target):
        MigrationExecutor(connection).migrate([target])
        return MigrationExecutor(connection).loader.project_state([target]).apps

    def test_existing_orders_keep_their_data_and_get_empty_snapshot_fields(self):
        old_apps = self._migrate(('orders', '0008_order_tracking_code'))
        OldOrder = old_apps.get_model('orders', 'Order')
        old = OldOrder.objects.create(first_name='علی', last_name='رضایی', phone='09121112233',
                                      address='تهران، خیابان آزادی، پلاک ۱', postal_code='1234567890',
                                      payment_method='cash', shipping_cost=200000, total_price=500000)

        new_apps = self._migrate(('orders', '0009_order_shipping_snapshot'))
        row = new_apps.get_model('orders', 'Order').objects.get(pk=old.pk)

        self.assertEqual((row.first_name, row.address, int(row.shipping_cost), int(row.total_price)),
                         ('علی', 'تهران، خیابان آزادی، پلاک ۱', 200000, 500000))
        self.assertEqual((row.province, row.city, row.zone, row.shipping_method, row.shipping_label), ('',) * 5)

        # سفارش قدیمی بعد از مایگریشن هم «آدرس کامل» درست نشان می‌دهد
        from orders.models import Order as RealOrder
        self.assertEqual(RealOrder.objects.get(pk=old.pk).full_address, 'تهران، خیابان آزادی، پلاک ۱')
