"""
موجودی قابل‌فروش و رزرو اتمیک (products/stock.py): فرمول، رزرو شرطی، مهلت/انقضا، آزادسازی پس از سینک، ممیزی و اتصال به
سبد/فهرست‌ها. (هم‌زمانیِ چندنخی: orders/tests_stock_concurrency.py)
"""
from datetime import timedelta

from django.core.cache import cache
from django.db import transaction
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.testing import make_approved_user
from cart.models import Cart, CartItem
from cart.services import add_item
from products import stock
from products.models import Category, Product, SiteSettings, StockReservation
from products.ordering import stock_first
from products.stock import InsufficientStock, compute_available


class ComputeAvailableTests(SimpleTestCase):
    def test_formula(self):
        self.assertEqual(compute_available(10, 3, 1), 6)
        self.assertEqual(compute_available(10, 0, 0), 10)

    def test_fractional_stock_is_floored_and_result_is_never_negative(self):
        self.assertEqual(compute_available(2.5, 0, 0), 2)
        self.assertEqual(compute_available(0.9, 0, 0), 0)
        self.assertEqual(compute_available(3, 5, 0), 0)
        self.assertEqual(compute_available(1, 0, 2), 0)
        self.assertEqual(compute_available(None, 0, 0), 0)


class StockBase(TestCase):
    def setUp(self):
        self.addCleanup(cache.delete, SiteSettings.CACHE_KEY)
        category = Category.objects.create(name='موجودی', slug='stock-cat')
        self.product = self.make_product('a', stock=5, category=category)
        self.other = self.make_product('b', stock=2, category=category)
        self.category = category

    def make_product(self, key, stock=5, category=None, **extra):
        return Product.objects.create(name=f'کالا {key}', slug=f'stock-{key}', erp_code=f'ERP-STOCK-{key}',
                                      category=category or self.category, price=100000, price2=90000, stock=stock, **extra)

    def set_buffer(self, value):
        settings_obj = SiteSettings.load()
        settings_obj.stock_safety_buffer = value
        settings_obj.save()

    def reload(self, product=None):
        product = product or self.product
        product.refresh_from_db()
        return product

    def reserve(self, order_id, lines, **kwargs):
        with transaction.atomic():
            stock.reserve_for_order(order_id, lines, **kwargs)


class ReserveTests(StockBase):
    def test_reserve_counts_and_writes_the_ledger(self):
        self.reserve(1, {self.product.pk: 2, self.other.pk: 1})
        self.assertEqual((self.reload().reserved_quantity, self.reload(self.other).reserved_quantity), (2, 1))
        rows = StockReservation.objects.filter(order_id=1)
        self.assertEqual({(r.product_id, r.quantity, r.state) for r in rows},
                         {(self.product.pk, 2, 'held'), (self.other.pk, 1, 'held')})

    def test_available_reflects_reservations_and_the_safety_buffer(self):
        self.reserve(1, {self.product.pk: 2})
        self.assertEqual(self.reload().available_quantity, 3)
        self.set_buffer(1)
        self.assertEqual(self.reload().available_quantity, 2)
        self.set_buffer(2)
        self.assertEqual(self.reload().available_quantity, 1)

    def test_last_unit_goes_to_the_first_order_only(self):
        Product.objects.filter(pk=self.product.pk).update(stock=1)
        self.reserve(1, {self.product.pk: 1})
        with self.assertRaises(InsufficientStock) as ctx:
            self.reserve(2, {self.product.pk: 1})
        shortage = ctx.exception.shortages[0]
        self.assertEqual((shortage['product'].pk, shortage['requested'], shortage['available']), (self.product.pk, 1, 0))
        self.assertEqual(self.reload().reserved_quantity, 1)
        self.assertFalse(StockReservation.objects.filter(order_id=2).exists())

    def test_a_shortage_on_any_line_rolls_back_the_whole_order(self):
        with self.assertRaises(InsufficientStock):
            with transaction.atomic():
                stock.reserve_for_order(7, {self.product.pk: 1, self.other.pk: 99})
        self.assertEqual((self.reload().reserved_quantity, self.reload(self.other).reserved_quantity), (0, 0))
        self.assertFalse(StockReservation.objects.filter(order_id=7).exists())

    def test_the_safety_buffer_blocks_the_units_it_holds_back(self):
        self.set_buffer(2)
        Product.objects.filter(pk=self.product.pk).update(stock=3)
        self.reserve(1, {self.product.pk: 1})                      # 3 - 2 = 1 قابل‌فروش
        with self.assertRaises(InsufficientStock):
            self.reserve(2, {self.product.pk: 1})

    def test_fractional_stock_never_allows_more_than_the_whole_part(self):
        Product.objects.filter(pk=self.product.pk).update(stock=2.5)
        with self.assertRaises(InsufficientStock):
            self.reserve(1, {self.product.pk: 3})
        self.reserve(2, {self.product.pk: 2})

    def test_reserving_outside_a_transaction_is_refused(self):
        # TestCase خودش داخل atomic است؛ برای شبیه‌سازی «خارج از تراکنش» بررسی را مستقیم می‌سنجیم
        from unittest import mock
        with mock.patch('products.stock.transaction.get_connection') as conn:
            conn.return_value.in_atomic_block = False
            with self.assertRaises(RuntimeError):
                stock.reserve_for_order(1, {self.product.pk: 1})

    def test_reserving_again_for_the_same_order_is_idempotent_and_refreshes_the_deadline(self):
        now = timezone.now()
        self.reserve(1, {self.product.pk: 2}, expires_at=now + timedelta(minutes=5), now=now)
        later = now + timedelta(minutes=4)
        self.reserve(1, {self.product.pk: 2}, expires_at=later + timedelta(minutes=20), now=later)
        self.assertEqual(self.reload().reserved_quantity, 2)
        row = StockReservation.objects.get(order_id=1)
        self.assertEqual(row.expires_at, later + timedelta(minutes=20))


class ExpiryTests(StockBase):
    def test_only_held_rows_with_a_past_deadline_expire(self):
        now = timezone.now()
        self.reserve(1, {self.product.pk: 1}, expires_at=now - timedelta(minutes=1), now=now - timedelta(minutes=30))
        self.reserve(2, {self.product.pk: 1}, expires_at=now + timedelta(minutes=10), now=now)
        self.reserve(3, {self.product.pk: 1}, expires_at=None, now=now)                 # چکی/پرداخت‌شده: بی‌مهلت
        self.assertEqual(stock.expire_stale(now=now), 1)
        states = {r.order_id: r.state for r in StockReservation.objects.all()}
        self.assertEqual(states, {1: 'expired', 2: 'held', 3: 'held'})
        self.assertEqual(self.reload().reserved_quantity, 2)

    def test_expiry_is_lazy_an_expired_hold_never_blocks_a_new_buyer(self):
        Product.objects.filter(pk=self.product.pk).update(stock=1)
        now = timezone.now()
        self.reserve(1, {self.product.pk: 1}, expires_at=now - timedelta(minutes=1), now=now - timedelta(minutes=30))
        self.reserve(2, {self.product.pk: 1}, expires_at=None, now=now)               # بدون هیچ تسک زمان‌بندی‌شده‌ای
        self.assertEqual(StockReservation.objects.get(order_id=1).state, 'expired')
        self.assertEqual(self.reload().reserved_quantity, 1)

    def test_an_expired_order_can_be_re_reserved_while_stock_remains(self):
        now = timezone.now()
        self.reserve(1, {self.product.pk: 2}, expires_at=now - timedelta(minutes=1), now=now - timedelta(minutes=30))
        stock.expire_stale(now=now)
        self.assertEqual(self.reload().reserved_quantity, 0)
        self.reserve(1, {self.product.pk: 2}, expires_at=None, now=now)
        row = StockReservation.objects.get(order_id=1)
        self.assertEqual((row.state, row.released_at, row.release_reason), ('held', None, ''))
        self.assertEqual(self.reload().reserved_quantity, 2)

    def test_re_reserving_an_expired_order_fails_when_someone_else_took_the_stock(self):
        Product.objects.filter(pk=self.product.pk).update(stock=1)
        now = timezone.now()
        self.reserve(1, {self.product.pk: 1}, expires_at=now - timedelta(minutes=1), now=now - timedelta(minutes=30))
        stock.expire_stale(now=now)
        self.reserve(2, {self.product.pk: 1})
        with self.assertRaises(InsufficientStock):
            self.reserve(1, {self.product.pk: 1})

    def test_extend_hold_restarts_the_twenty_minute_window_only_for_deadline_rows(self):
        now = timezone.now()
        self.reserve(1, {self.product.pk: 1}, expires_at=now + timedelta(minutes=1), now=now)
        self.reserve(2, {self.product.pk: 1}, expires_at=None, now=now)
        stock.extend_hold(1, now=now)
        stock.extend_hold(2, now=now)
        self.assertEqual(StockReservation.objects.get(order_id=1).expires_at, now + stock.RESERVATION_TTL)
        self.assertIsNone(StockReservation.objects.get(order_id=2).expires_at)

    def test_ttl_is_twenty_minutes(self):
        self.assertEqual(stock.RESERVATION_TTL, timedelta(minutes=20))

    def test_confirm_hold_removes_the_deadline(self):
        now = timezone.now()
        self.reserve(1, {self.product.pk: 1}, expires_at=now - timedelta(minutes=1), now=now - timedelta(minutes=30))
        self.assertEqual(stock.confirm_hold(1), 1)
        self.assertEqual(stock.expire_stale(now=now), 0)
        self.assertEqual(StockReservation.objects.get(order_id=1).state, 'held')


class ReleaseTests(StockBase):
    def test_release_order_frees_the_counter_once(self):
        self.reserve(1, {self.product.pk: 2, self.other.pk: 1})
        self.assertEqual(stock.release_order(1, 'order_canceled'), 2)
        self.assertEqual(stock.release_order(1, 'order_canceled'), 0)                   # بار دوم کاری ندارد
        self.assertEqual((self.reload().reserved_quantity, self.reload(self.other).reserved_quantity), (0, 0))
        self.assertEqual({r.release_reason for r in StockReservation.objects.all()}, {'order_canceled'})

    def test_counter_never_goes_below_zero(self):
        Product.objects.filter(pk=self.product.pk).update(reserved_quantity=0)
        stock._unclaim(self.product.pk, 5)
        self.assertEqual(self.reload().reserved_quantity, 0)

    def test_invoiced_reservation_stays_until_a_later_sync_updates_the_stock(self):
        t0 = timezone.now()
        self.reserve(1, {self.product.pk: 2})
        stock.mark_invoiced(1, now=t0)
        row = StockReservation.objects.get(order_id=1)
        self.assertEqual((row.state, row.invoiced_at, row.expires_at), ('invoiced', t0, None))

        # بدون سینک (یا سینکِ قدیمی‌تر از فاکتور): رزرو می‌ماند
        self.assertEqual(stock.release_synced(), 0)
        Product.objects.filter(pk=self.product.pk).update(stock_synced_at=t0 - timedelta(minutes=1))
        self.assertEqual(stock.release_synced(), 0)
        self.assertEqual(self.reload().reserved_quantity, 2)

        # سینکی که بعد از ثبت فاکتور شروع شده: آزاد می‌شود
        Product.objects.filter(pk=self.product.pk).update(stock_synced_at=t0 + timedelta(seconds=1))
        self.assertEqual(stock.release_synced(), 1)
        self.assertEqual(self.reload().reserved_quantity, 0)
        self.assertEqual(StockReservation.objects.get(order_id=1).release_reason, 'synced')

    def test_invoiced_reservation_is_not_counted_twice_it_still_blocks_buyers_until_released(self):
        Product.objects.filter(pk=self.product.pk).update(stock=2)
        self.reserve(1, {self.product.pk: 2})
        stock.mark_invoiced(1)
        with self.assertRaises(InsufficientStock):
            self.reserve(2, {self.product.pk: 1})


class AuditTests(StockBase):
    def test_counter_matches_the_ledger_when_healthy(self):
        self.reserve(1, {self.product.pk: 2})
        self.reserve(2, {self.product.pk: 1, self.other.pk: 1})
        stock.release_order(2, 'x')
        self.assertEqual(stock.audit_reserved_counters(), [])

    def test_a_drifted_counter_is_reported_and_can_be_repaired_from_the_ledger(self):
        self.reserve(1, {self.product.pk: 2})
        Product.objects.filter(pk=self.product.pk).update(reserved_quantity=7)
        Product.objects.filter(pk=self.other.pk).update(reserved_quantity=1)           # شمارنده بدون دفتر
        found = stock.audit_reserved_counters()
        self.assertEqual({(m['product_id'], m['counter'], m['ledger']) for m in found},
                         {(self.product.pk, 7, 2), (self.other.pk, 1, 0)})
        stock.audit_reserved_counters(fix=True)
        self.assertEqual((self.reload().reserved_quantity, self.reload(self.other).reserved_quantity), (2, 0))
        self.assertEqual(stock.audit_reserved_counters(), [])


class StorefrontAvailabilityTests(StockBase):
    def test_cart_cannot_exceed_the_sellable_quantity(self):
        user = make_approved_user('09120000501')
        cart = Cart.objects.create(user=user)
        Product.objects.filter(pk=self.product.pk).update(stock=3)
        self.reserve(1, {self.product.pk: 2})
        product = self.reload()                                                         # قابل‌فروش = 1
        add_item(cart, product)
        add_item(cart, product)                                                         # سقف: بدون خطا و بدون افزایش
        self.assertEqual(CartItem.objects.get(cart=cart).quantity, 1)

    def test_a_fully_reserved_product_cannot_enter_the_cart(self):
        user = make_approved_user('09120000502')
        cart = Cart.objects.create(user=user)
        Product.objects.filter(pk=self.product.pk).update(stock=1)
        self.reserve(1, {self.product.pk: 1})
        self.assertIsNone(add_item(cart, self.reload()))

    def test_stock_first_pushes_fully_reserved_products_after_available_ones(self):
        Product.objects.filter(pk=self.product.pk).update(stock=1)
        self.reserve(1, {self.product.pk: 1})
        names = [p.pk for p in stock_first(Product.objects.filter(pk__in=[self.product.pk, self.other.pk]), 'id')]
        self.assertEqual(names, [self.other.pk, self.product.pk])
        self.set_buffer(2)                                                              # بافر: «other» (۲ عدد) هم ناموجود
        names = [p.pk for p in stock_first(Product.objects.filter(pk__in=[self.product.pk, self.other.pk]), 'id')]
        self.assertEqual(names, [self.product.pk, self.other.pk])

    def test_in_stock_filter_and_product_page_use_the_sellable_quantity(self):
        Product.objects.filter(pk=self.product.pk).update(stock=1)
        self.reserve(1, {self.product.pk: 1})
        listing = self.client.get(reverse('products:product_list'), {'in_stock': '1'})
        shown = {p.pk for p in listing.context['products']} if 'products' in listing.context else set()
        self.assertNotIn(self.product.pk, shown)
        self.assertIn(self.other.pk, shown)
        page = self.client.get(reverse('products:product_detail', args=[self.product.slug]))
        self.assertContains(page, 'schema.org/OutOfStock')
        page = self.client.get(reverse('products:product_detail', args=[self.other.slug]))
        self.assertContains(page, 'schema.org/InStock')

    def test_safety_buffer_field_defaults_to_zero_and_is_editable_in_the_site_settings_admin(self):
        self.assertEqual(SiteSettings.load().stock_safety_buffer, 0)
        from products.admin import SiteSettingsAdmin
        fields = {f for _, opts in SiteSettingsAdmin.fieldsets for f in opts['fields']}
        self.assertIn('stock_safety_buffer', fields)
        self.assertEqual([c[0] for c in SiteSettings.STOCK_SAFETY_BUFFER_CHOICES], [0, 1, 2])
