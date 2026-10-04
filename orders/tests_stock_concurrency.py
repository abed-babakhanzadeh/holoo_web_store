"""
تست هم‌زمانی چندنخی رزرو موجودی روی دیتابیس واقعی (SQL Server، تراکنش‌های واقعی و commit شده) — هم‌الگوی
tests_coupon_concurrency. تضمین‌ها:
  - آخرین قلم: از N خریدار هم‌زمان دقیقاً یکی برنده می‌شود؛ بقیه ۴۰۹ می‌گیرند و هیچ سفارش/رزرو/تغییر سبدی نمی‌سازند.
  - موجودی K: دقیقاً K سفارش یک‌تایی پذیرفته می‌شود و reserved_quantity دقیقاً K می‌شود (نه بیشتر، نه کمتر).
  - سبدهای چندقلمی با ترتیب متفاوت کالا deadlock نمی‌سازند.
  - سینک هلو که هم‌زمان stock را می‌نویسد، reserved_quantity را هرگز خراب نمی‌کند (lost update).
"""

from datetime import timedelta

from django.utils import timezone

from accounts.models import Address
from accounts.testing import make_approved_user
from cart.models import Cart, CartItem
from holoo.product_state import apply_holoo_product_state
from orders.models import Order, OrderItem
from orders.tests_coupon_concurrency import CouponConcurrencyBase
from products.models import Product, StockReservation


class StockRaceTests(CouponConcurrencyBase):
    def make_buyer(self, index, items):
        """ items: [(product, quantity)] به همان ترتیب درج در سبد """
        user = make_approved_user(f'0912099{index:04d}', price_level=1)
        cart = Cart.objects.create(user=user)
        for product, quantity in items:
            CartItem.objects.create(cart=cart, product=product, quantity=quantity)
        address = Address.objects.create(user=user, title='خانه', receiver_first_name='الف', receiver_last_name='ب',
                                         receiver_phone='09123334455', city=self.city, postal_code='1112223334', address='خیابان رقابت')
        return user, address

    def race(self, buyers, expected):
        results = self.run_threads([self.submit_job(user, address, None, expected) for user, address in buyers])
        errors = [r for r in results if isinstance(r, BaseException)]
        self.assertEqual(errors, [], f'خطای غیرمنتظره در نخ‌ها: {errors}')
        return results

    def reserved(self, product):
        product.refresh_from_db()
        return product.reserved_quantity

    def active_rows(self, product):
        return StockReservation.objects.filter(product=product, state__in=('held', 'invoiced')).count()

    def test_the_last_unit_goes_to_exactly_one_of_many_simultaneous_buyers(self):
        Product.objects.filter(pk=self.product.pk).update(stock=1)
        buyers = [self.make_buyer(i, [(self.product, 1)]) for i in range(6)]
        results = self.race(buyers, expected=100000)

        self.assertEqual(sorted(r.status_code for r in results), [302] + [409] * 5)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(self.reserved(self.product), 1)
        self.assertEqual(self.active_rows(self.product), 1)
        self.assertEqual(Cart.objects.count(), 5)                       # بازنده‌ها سبدشان را نگه داشتند

    def test_stock_k_accepts_exactly_k_orders(self):
        Product.objects.filter(pk=self.product.pk).update(stock=4)
        buyers = [self.make_buyer(i, [(self.product, 1)]) for i in range(10)]
        results = self.race(buyers, expected=100000)

        self.assertEqual(sorted(r.status_code for r in results), [302] * 4 + [409] * 6)
        self.assertEqual(Order.objects.count(), 4)
        self.assertEqual(self.reserved(self.product), 4)
        self.assertEqual(OrderItem.objects.count(), 4)

    def test_multi_item_carts_in_opposite_order_never_deadlock(self):
        other = Product.objects.create(name='کالای دوم', slug='race-p2', erp_code='ERP-RACE-2', category=self.product.category,
                                       price=100000, price2=90000, stock=1)
        Product.objects.filter(pk=self.product.pk).update(stock=1)
        buyers = [self.make_buyer(0, [(self.product, 1), (other, 1)]), self.make_buyer(1, [(other, 1), (self.product, 1)])]
        results = self.race(buyers, expected=200000)

        self.assertEqual(sorted(r.status_code for r in results), [302, 409])
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual((self.reserved(self.product), self.reserved(other)), (1, 1))

    def test_a_concurrent_holoo_sync_never_overwrites_the_reservation_counter(self):
        Product.objects.filter(pk=self.product.pk).update(stock=50)
        buyers = [self.make_buyer(i, [(self.product, 1)]) for i in range(5)]
        base = timezone.now()
        row = {'Name': self.product.name, 'Code': 'C1', 'Few': 50, 'SellPrice': 100000, 'SellPrice2': 90000}

        def sync_job():
            for step in range(15):
                product = Product.objects.get(pk=self.product.pk)          # نسخه‌ی «قدیمی» حافظه، دقیقاً مثل حلقه‌ی سینک
                apply_holoo_product_state(product, row, base + timedelta(seconds=step + 1))
            return 'synced'

        jobs = [self.submit_job(user, address, None, 100000) for user, address in buyers] + [sync_job]
        results = self.run_threads(jobs)
        errors = [r for r in results if isinstance(r, BaseException)]
        self.assertEqual(errors, [], f'خطای غیرمنتظره در نخ‌ها: {errors}')

        self.assertEqual(Order.objects.count(), 5)
        self.assertEqual(self.reserved(self.product), 5)
        self.assertEqual(self.active_rows(self.product), 5)
