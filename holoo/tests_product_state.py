"""
نویسنده‌ی واحد وضعیت کالا (holoo/product_state.py) و اتصال سینک به رزرو (stock_synced_at، آزادسازی رزرو فاکتورشده).
"""
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from holoo.product_state import apply_holoo_product_state, row_values
from holoo.tasks import sync_products_from_holoo
from products import stock
from products.models import Category, Product, StockReservation


def holoo_row(erp='ERP-PS-1', **extra):
    row = {
        'ErpCode': erp, 'Name': 'کالای تست', 'Code': 'C-1', 'Few': 10, 'SellPrice': 100000, 'SellPrice2': 90000,
        'SellPrice3': 0, 'SellPrice4': 0, 'SellPrice5': 0, 'SellPrice6': 0, 'SellPrice7': 0, 'SellPrice8': 0,
        'SellPrice9': 0, 'SellPrice10': 0, 'IsActive': True, 'service': False,
    }
    row.update(extra)
    return row


class ProductStateWriterTests(TestCase):
    def setUp(self):
        category = Category.objects.create(name='وضعیت', slug='state-cat')
        self.product = Product.objects.create(name='قدیمی', slug='state-p', erp_code='ERP-PS-1', category=category,
                                              price=1, stock=1)

    def test_row_values_rounds_nothing_here_but_maps_every_owned_column(self):
        values = row_values(holoo_row(SellPrice3=41000, Few='7'))
        self.assertEqual((values['price'], values['price2'], values['price3'], values['stock'], values['product_code']),
                         (100000.0, 90000.0, 41000.0, 7.0, 'C-1'))
        self.assertEqual(set(values), {'price', 'stock', 'product_code', *[f'price{i}' for i in range(2, 11)]})

    def test_apply_writes_holoo_owned_columns_and_stamps_the_observation(self):
        t = timezone.now()
        self.assertTrue(apply_holoo_product_state(self.product, holoo_row(Few=10, Name='جدید'), t))
        self.product.refresh_from_db()
        self.assertEqual((self.product.name, self.product.stock, self.product.price, self.product.price2), ('جدید', 10, 100000, 90000))
        self.assertEqual((self.product.stock_synced_at, self.product.price_synced_at), (t, t))
        self.assertEqual(self.product.name_normalized, 'جدید')

    def test_prices_are_rounded_to_the_nearest_toman_by_the_column(self):
        apply_holoo_product_state(self.product, holoo_row(SellPrice=6000.81, SellPrice2=37000.5, SellPrice3=125800.43), timezone.now())
        self.product.refresh_from_db()
        self.assertEqual((self.product.price, self.product.price2, self.product.price3), (6001, 37001, 125800))

    def test_reserved_quantity_is_never_touched_even_with_a_stale_in_memory_copy(self):
        stale = Product.objects.get(pk=self.product.pk)                        # دقیقاً مثل حلقه‌ی سینک: نسخه‌ی حافظه
        Product.objects.filter(pk=self.product.pk).update(reserved_quantity=3)  # رزرو سفارشی هم‌زمان
        apply_holoo_product_state(stale, holoo_row(Few=50), timezone.now())
        self.product.refresh_from_db()
        self.assertEqual((self.product.stock, self.product.reserved_quantity), (50, 3))

    def test_older_observation_never_overwrites_a_newer_one(self):
        t = timezone.now()
        apply_holoo_product_state(self.product, holoo_row(Few=10), t)
        stale = Product.objects.get(pk=self.product.pk)
        self.assertFalse(apply_holoo_product_state(stale, holoo_row(Few=99), t - timedelta(seconds=30)))
        self.product.refresh_from_db()
        self.assertEqual((self.product.stock, self.product.stock_synced_at), (10, t))
        self.assertTrue(apply_holoo_product_state(stale, holoo_row(Few=11), t + timedelta(seconds=1)))
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 11)


class SyncIntegrationTests(TestCase):
    def setUp(self):
        cache.delete('lock:holoo:product_sync')
        category = Category.objects.create(name='سینک', slug='sync-int-cat')
        self.product = Product.objects.create(name='کالا', slug='sync-int-p', erp_code='ERP-PS-1', category=category,
                                              price=1, stock=5)

    def run_sync(self, rows):
        cache.delete('lock:holoo:product_sync')
        with mock.patch('holoo.client.HolooClient.get_product_count', return_value=len(rows)), \
             mock.patch('holoo.client.HolooClient.get_products', return_value={'product': rows}):
            return sync_products_from_holoo()

    def test_sync_stamps_new_and_existing_products_and_keeps_reservations(self):
        with stock.transaction.atomic():
            stock.reserve_for_order(1, {self.product.pk: 2})
        before = timezone.now()
        self.run_sync([holoo_row('ERP-PS-1', Few=8), holoo_row('ERP-PS-2', Few=3, Name='تازه', Code='C-2')])
        self.product.refresh_from_db()
        new = Product.objects.get(erp_code='ERP-PS-2')
        self.assertEqual((self.product.stock, self.product.reserved_quantity), (8, 2))
        self.assertGreaterEqual(self.product.stock_synced_at, before)
        self.assertGreaterEqual(new.stock_synced_at, before)
        self.assertEqual((new.stock, new.reserved_quantity), (3, 0))

    def test_sync_releases_an_invoiced_reservation_only_after_a_later_observation(self):
        with stock.transaction.atomic():
            stock.reserve_for_order(1, {self.product.pk: 2})
        stock.mark_invoiced(1, now=timezone.now() + timedelta(minutes=5))            # فاکتور «بعد از» لحظه‌ی واکشیِ سینک
        self.run_sync([holoo_row('ERP-PS-1', Few=8)])
        self.assertEqual(StockReservation.objects.get(order_id=1).state, 'invoiced')
        self.assertEqual(Product.objects.get(pk=self.product.pk).reserved_quantity, 2)

        StockReservation.objects.filter(order_id=1).update(invoiced_at=timezone.now() - timedelta(minutes=5))
        self.run_sync([holoo_row('ERP-PS-1', Few=6)])                                # سینکِ بعد از فاکتور
        self.assertEqual(StockReservation.objects.get(order_id=1).state, 'released')
        self.assertEqual(Product.objects.get(pk=self.product.pk).reserved_quantity, 0)
