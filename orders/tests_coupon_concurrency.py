"""
تست هم‌زمانی چندنخی ثبت سفارش با کد تخفیف روی دیتابیس واقعی (SQL Server، تراکنش‌های واقعی و commit شده).

هر نخ کلاینت و اتصال دیتابیس مخصوص خودش را دارد و با یک Barrier هم‌زمان شروع می‌کنند. تضمین‌ها:
  - با سقف کل K، دقیقاً K سفارش کد را می‌گیرند؛ بقیه ۴۰۹ (مبلغ عوض شد) می‌گیرند و هیچ سفارش/رزرویی نمی‌سازند.
  - سقف هر کاربر با درخواست‌های هم‌زمانِ یک کاربر هم رعایت می‌شود و سفارش دوباره ساخته نمی‌شود.
  - هم‌زمانیِ ثبت سفارش با لغو/پرداخت، ظرفیت را خراب نمی‌کند.
"""

import threading
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test import Client, TransactionTestCase
from django.urls import reverse

from accounts.models import Address
from accounts.testing import make_approved_user
from cart.models import Cart, CartItem
from locations.models import City, Province
from orders.models import Order
from products.models import Category, Product, SiteSettings
from promotions import coupons
from promotions.models import CouponRedemption, DiscountPolicy
from promotions.testing import make_coupon, reset_coupon_attempts, reset_promotions_cache


class CouponConcurrencyBase(TransactionTestCase):
    # TransactionTestCase داده‌های مهاجرت (استان/شهرها) را flush می‌کند؛ با این پرچم بازیابی می‌شود
    serialized_rollback = True

    def setUp(self):
        self.addCleanup(cache.delete, SiteSettings.CACHE_KEY)
        reset_promotions_cache()
        reset_coupon_attempts()
        self.addCleanup(reset_promotions_cache)
        self.addCleanup(reset_coupon_attempts)
        DiscountPolicy.load()
        SiteSettings.load()
        province = Province.objects.create(name='استان هم‌زمانی')
        self.city = City.objects.create(province=province, name='شهر هم‌زمانی')
        category = Category.objects.create(name='هم‌زمانی', slug='race-cat')
        self.product = Product.objects.create(name='کالای رقابتی', slug='race-p', erp_code='ERP-RACE', category=category,
                                              price=100000, price2=90000, stock=100)

    def new_buyer(self, index):
        user = make_approved_user(f'0912088{index:04d}', price_level=1)
        cart = Cart.objects.create(user=user)
        CartItem.objects.create(cart=cart, product=self.product, quantity=1)
        address = Address.objects.create(user=user, title='خانه', receiver_first_name='الف', receiver_last_name='ب',
                                         receiver_phone='09123334455', city=self.city, postal_code='1112223334', address='خیابان رقابت')
        return user, address

    def run_threads(self, jobs):
        """ jobs: فهرست تابع‌های بدون آرگومان؛ همه با Barrier هم‌زمان شروع می‌شوند. خروجی: نتیجه‌ی هرکدام یا استثنا """
        barrier = threading.Barrier(len(jobs))
        results = [None] * len(jobs)

        def worker(index, job):
            try:
                barrier.wait(timeout=30)
                results[index] = job()
            except BaseException as error:                               # noqa: BLE001 - نتیجه‌ی استثنا هم ثبت می‌شود
                results[index] = error
            finally:
                connection.close()                                       # اتصال مخصوص همین نخ

        threads = [threading.Thread(target=worker, args=(i, job)) for i, job in enumerate(jobs)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        self.assertFalse(any(t.is_alive() for t in threads), 'یک نخ گیر کرد (احتمال deadlock)')
        return results

    def submit_job(self, user, address, code, expected):
        def job():
            client = Client()
            client.force_login(user)
            session = client.session
            if code:
                session[coupons.SESSION_KEY] = code
            session.save()
            return client.post(reverse('orders:submit_order'), {
                'address_id': address.pk, 'payment_method': 'check', 'expected_total': expected,
            })
        return job

    def place_concurrently(self, buyers, code, expected):
        with mock.patch('holoo.receivers.send_order_to_holoo'):
            results = self.run_threads([self.submit_job(user, address, code, expected) for user, address in buyers])
        errors = [r for r in results if isinstance(r, BaseException)]
        self.assertEqual(errors, [], f'خطای غیرمنتظره در نخ‌ها: {errors}')
        return results


class CouponTotalLimitRaceTests(CouponConcurrencyBase):
    def test_total_limit_one_lets_exactly_one_of_many_simultaneous_orders_use_the_code(self):
        make_coupon('RACE1', value=10, total_limit=1, per_user_limit=None)
        buyers = [self.new_buyer(i) for i in range(8)]
        results = self.place_concurrently(buyers, 'RACE1', expected=90000)

        statuses = sorted(r.status_code for r in results)
        self.assertEqual(statuses, [302] + [409] * 7)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Order.objects.get().coupon_code, 'RACE1')
        self.assertEqual(Order.objects.get().total_price, 90000)
        redemptions = CouponRedemption.objects.all()
        self.assertEqual((redemptions.count(), redemptions.get().status), (1, 'reserved'))
        # بازنده‌ها هیچ سفارش/رزرویی نساختند و سبدشان دست‌نخورده ماند
        self.assertEqual(Cart.objects.count(), 7)

    def test_total_limit_three_of_ten(self):
        make_coupon('RACE3', value=10, total_limit=3, per_user_limit=None)
        buyers = [self.new_buyer(i) for i in range(10)]
        results = self.place_concurrently(buyers, 'RACE3', expected=90000)
        self.assertEqual(sorted(r.status_code for r in results), [302] * 3 + [409] * 7)
        self.assertEqual(Order.objects.filter(coupon_code='RACE3').count(), 3)
        self.assertEqual(CouponRedemption.objects.count(), 3)
        self.assertEqual(coupons.active_uses(coupons.find_coupon('RACE3')), 3)

    def test_orders_without_a_code_are_never_blocked_by_the_race(self):
        make_coupon('RACE4', value=10, total_limit=1, per_user_limit=None)
        winners = [self.new_buyer(i) for i in range(3)]
        plain = [self.new_buyer(i) for i in range(10, 13)]
        with mock.patch('holoo.receivers.send_order_to_holoo'):
            results = self.run_threads(
                [self.submit_job(u, a, 'RACE4', 90000) for u, a in winners] + [self.submit_job(u, a, None, 100000) for u, a in plain])
        self.assertEqual(sum(1 for r in results if r.status_code == 302), 1 + 3)      # ۱ برنده‌ی کد + ۳ سفارش بدون کد
        self.assertEqual(CouponRedemption.objects.count(), 1)

    def test_free_shipping_coupon_race(self):
        from locations.models import DeliveryZone
        make_coupon('RACESHIP', kind='free_shipping', total_limit=2, per_user_limit=None)
        users = [self.new_buyer(i)[0] for i in range(6)]                    # اول آدرس‌های ساده؛ بعد شهر ناحیه‌دار می‌شود
        zone = DeliveryZone.objects.create(city=self.city, name='ناحیه‌ی رقابت', shipping_cost=45000)
        buyers = []
        for user in users:
            address = Address.objects.create(user=user, title='پیکی', receiver_first_name='الف', receiver_last_name='ب',
                                             receiver_phone='09123334455', city=self.city, zone=zone, postal_code='1112223334', address='x')
            buyers.append((user, address))
        # برنده: ارسال رایگان ← ۱۰۰٬۰۰۰؛ بازنده: کرایه‌ی ۴۵٬۰۰۰ ← ۱۴۵٬۰۰۰ (مبلغ نمایش‌داده‌شده فرق می‌کند ← ۴۰۹)
        results = self.place_concurrently(buyers, 'RACESHIP', expected=100000)
        self.assertEqual(sorted(r.status_code for r in results), [302] * 2 + [409] * 4)
        self.assertEqual(CouponRedemption.objects.count(), 2)
        self.assertEqual(Order.objects.filter(shipping_discount=45000).count(), 2)


class CouponPerUserRaceTests(CouponConcurrencyBase):
    def test_one_user_double_submitting_from_two_sessions_creates_one_order(self):
        make_coupon('SAMEUSER', value=10, per_user_limit=1, total_limit=None)
        user, address = self.new_buyer(1)
        results = self.place_concurrently([(user, address)] * 4, 'SAMEUSER', expected=90000)
        self.assertEqual(Order.objects.filter(user=user).count(), 1)                      # قفل سبد: فقط یک سفارش
        self.assertEqual(CouponRedemption.objects.filter(user=user).count(), 1)
        self.assertEqual(sum(1 for r in results if r.status_code == 302), 4)               # بقیه به تاریخچه هدایت شدند
        self.assertEqual(sorted({r.status_code for r in results}), [302])

    def test_per_user_limit_holds_when_the_same_user_races_with_two_carts_over_time(self):
        """ سفارش دوم همان کاربر (سبد تازه) بعد از رزرو اول دیگر کد نمی‌گیرد، حتی اگر پرداخت نشده باشد """
        make_coupon('PERUSER', value=10, per_user_limit=1, total_limit=None)
        user, address = self.new_buyer(2)
        with mock.patch('holoo.receivers.send_order_to_holoo'):
            self.submit_job(user, address, 'PERUSER', 90000)()
        cart = Cart.objects.create(user=user)
        CartItem.objects.create(cart=cart, product=self.product, quantity=1)
        with mock.patch('holoo.receivers.send_order_to_holoo'):
            second = self.submit_job(user, address, 'PERUSER', 90000)()
        self.assertEqual(second.status_code, 409)
        self.assertEqual(Order.objects.filter(user=user).count(), 1)


class CouponCancelAndPayRaceTests(CouponConcurrencyBase):
    def test_cancellations_racing_with_new_orders_never_exceed_the_limit(self):
        """ ظرفیت ۱؛ سفارش اول لغو می‌شود درحالی‌که چند کاربر هم‌زمان کد را می‌خواهند: در پایان حداکثر یک استفاده‌ی فعال """
        coupon = make_coupon('CANCELRACE', value=10, total_limit=1, per_user_limit=None)
        first_user, first_address = self.new_buyer(1)
        with mock.patch('holoo.receivers.send_order_to_holoo'):
            self.submit_job(first_user, first_address, 'CANCELRACE', 90000)()
        first_order = Order.objects.get(user=first_user)
        buyers = [self.new_buyer(i) for i in range(2, 8)]

        def cancel():
            order = Order.objects.get(pk=first_order.pk)
            order.status = 'canceled'
            order.save()
            return 'canceled'

        with mock.patch('holoo.receivers.send_order_to_holoo'):
            results = self.run_threads([cancel] + [self.submit_job(u, a, 'CANCELRACE', 90000) for u, a in buyers])
        self.assertEqual([r for r in results if isinstance(r, BaseException)], [])
        self.assertLessEqual(coupons.active_uses(coupon), 1)                              # هرگز بیشتر از سقف
        winners = Order.objects.filter(coupon_code='CANCELRACE').exclude(pk=first_order.pk).count()
        self.assertLessEqual(winners, 1)

    def test_payment_redeem_and_release_running_together_end_in_a_consistent_state(self):
        coupon = make_coupon('PAYRACE', value=10, total_limit=5, per_user_limit=None)
        users = [self.new_buyer(i) for i in range(1, 5)]
        with mock.patch('holoo.receivers.send_order_to_holoo'):
            self.run_threads([self.submit_job(u, a, 'PAYRACE', 90000) for u, a in users])
        orders = list(Order.objects.filter(coupon_code='PAYRACE'))
        self.assertEqual(len(orders), 4)
        jobs = []
        for index, order in enumerate(orders):
            jobs.append((lambda o=order: coupons.redeem_for_order(o.pk)) if index % 2 == 0
                        else (lambda o=order: coupons.release_for_order(o.pk, 'order_canceled')))
        results = self.run_threads(jobs)
        self.assertEqual([r for r in results if isinstance(r, BaseException)], [])
        states = sorted(CouponRedemption.objects.values_list('status', flat=True))
        self.assertEqual(states, ['redeemed', 'redeemed', 'released', 'released'])
        self.assertEqual(coupons.active_uses(coupon), 2)
