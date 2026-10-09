"""
فاز G1: سقف اعتبار خرید چکی — فرمول اعتبار درگیر/مانده، گیت سمت سرور در ثبت سفارش (۴۰۹، سبد دست‌نخورده)، رفتار NULL/۰، آزادسازی با لغو،
هم‌زمانی (قفل ردیف کاربر)، مایگریشن داده‌ی سقف‌های قدیمی و فیلد سقف در ادمین کاربر.
"""
import threading
import time
from datetime import timedelta
from decimal import Decimal
from importlib import import_module
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import Client, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts import cheque_credit_service as credit_service
from accounts.cheque_credit import ChequeCreditRequest
from accounts.models import Address, CustomUser
from accounts.testing import make_approved_user
from cart.models import Cart, CartItem
from orders import credit, deadline
from orders.credit import CreditState
from orders.models import Order
from orders.tests import CheckoutTestBase
from orders.tests_payment_options import set_policy
from products.pricing import VIP_CHEQUE_VIP_PRICE


def set_limit(user, value):
    CustomUser.objects.filter(pk=user.pk).update(cheque_credit_limit=value)


def make_order(user, total, *, status='pending', method='check', **fields):
    return Order.objects.create(user=user, first_name='علی', last_name='رضایی', phone='09120000021', address='تهران',
                                payment_method=method, total_price=total, status=status, **fields)


# ------------------------------------------------------------------ فرمول
class CreditStateTests(TestCase):
    def test_unlimited(self):
        state = CreditState(limit=None, used=Decimal('500'))
        self.assertTrue(state.unlimited)
        self.assertIsNone(state.remaining)
        self.assertTrue(state.allows(10 ** 12))
        self.assertEqual(state.excess_for(10 ** 12), 0)

    def test_a_positive_limit(self):
        state = CreditState(limit=Decimal('1000'), used=Decimal('400'))
        self.assertEqual(state.remaining, 600)
        self.assertTrue(state.allows(600))                       # برابر مانده جا می‌شود
        self.assertFalse(state.allows(601))
        self.assertEqual(state.excess_for(750), 150)

    def test_remaining_is_never_negative(self):
        state = CreditState(limit=Decimal('1000'), used=Decimal('1500'))
        self.assertEqual(state.remaining, 0)
        self.assertEqual(state.excess_for(100), 100)

    def test_zero_is_a_freeze_not_unlimited(self):
        state = CreditState(limit=Decimal('0'), used=Decimal('0'))
        self.assertTrue(state.frozen)
        self.assertFalse(state.unlimited)
        self.assertFalse(state.allows(1))
        self.assertIn('فریز', credit.exceeded_message(state, 1))


class OutstandingTests(CheckoutTestBase):
    def used(self, user=None):
        return credit.outstanding_total(user or self.user)

    def test_only_active_cheque_orders_count(self):
        for status in ('pending', 'registered', 'processing', 'shipped', 'delivered'):
            make_order(self.user, 1000, status=status)
        make_order(self.user, 5000, status='canceled')
        make_order(self.user, 7000, status='rejected_stock')
        make_order(self.user, 9000, method='cash', settlement='online')           # نقدی/آنلاین جزو اعتبار چکی نیست
        self.assertEqual(self.used(), 5000)

    def test_a_pending_order_without_any_cheque_already_counts(self):
        make_order(self.user, 1234)
        self.assertEqual(self.used(), 1234)

    def test_a_vip_cheque_order_counts_too(self):
        make_order(self.user, 2000, method='vip', settlement='cheque')
        self.assertEqual(self.used(), 2000)

    def test_other_users_orders_are_ignored(self):
        make_order(self.other, 4000)
        self.assertEqual(self.used(), 0)
        self.assertEqual(self.used(self.other), 4000)

    def test_no_orders_is_zero(self):
        self.assertEqual(self.used(), 0)

    def test_cancelling_releases_the_credit(self):
        order = make_order(self.user, 3000)
        self.assertEqual(self.used(), 3000)
        order.status = 'canceled'
        order.save()
        self.assertEqual(self.used(), 0)

    def test_the_cheque_deadline_auto_cancel_releases_the_credit(self):
        order = make_order(self.user, 3000, cheque_deadline_at=timezone.now() - timedelta(hours=1))
        self.assertEqual(self.used(), 3000)
        self.assertEqual(deadline.cancel_expired_cheque_orders(), 1)
        self.assertEqual(self.used(), 0)
        self.assertEqual(Order.objects.get(pk=order.pk).status, 'canceled')

    def test_a_stock_rejection_releases_the_credit(self):
        order = make_order(self.user, 3000)
        Order.objects.filter(pk=order.pk).update(status='rejected_stock')
        self.assertEqual(self.used(), 0)

    def test_it_is_one_aggregate_query_and_uses_the_index(self):
        for _ in range(5):
            make_order(self.user, 100)
        with CaptureQueriesContext(connection) as captured:
            credit.outstanding_total(self.user)
        self.assertEqual(len(captured), 1)
        self.assertIn('order_user_settle_status_idx', [i.name for i in Order._meta.indexes])

    def test_credit_state_reads_the_users_limit(self):
        set_limit(self.user, 10000)
        make_order(self.user, 2500)
        state = credit.credit_state(CustomUser.objects.get(pk=self.user.pk))
        self.assertEqual((state.limit, state.used, state.remaining), (10000, 2500, 7500))


# ------------------------------------------------------------------ گیت سمت سرور
class GateTests(CheckoutTestBase):
    def place(self, option='check', **extra):
        data = {'address_id': self.address.pk, 'payment_method': option}
        data.update(extra)
        with mock.patch('holoo.receivers.send_order_to_holoo'):
            with self.captureOnCommitCallbacks(execute=True):
                return self.post_order(data)

    def cart_state(self):
        return Cart.objects.filter(user=self.user).count(), CartItem.objects.filter(cart__user=self.user).count()

    def test_a_user_without_a_limit_is_unaffected(self):
        self.assertIsNone(CustomUser.objects.get(pk=self.user.pk).cheque_credit_limit)
        response = self.place()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Order.objects.filter(user=self.user).count(), 1)

    def test_an_order_within_the_limit_is_accepted(self):
        set_limit(self.user, 1_000_000)
        response = self.place()
        self.assertEqual(response.status_code, 302)
        order = Order.objects.get(user=self.user)
        self.assertEqual(credit.outstanding_total(self.user), order.total_price)

    def test_an_order_exactly_equal_to_the_remaining_credit_is_accepted(self):
        total = self.current_total({'payment_method': 'check', 'address_id': self.address.pk})
        set_limit(self.user, total)
        self.assertEqual(self.place().status_code, 302)

    def test_one_toman_over_the_limit_is_refused_with_409_and_the_cart_survives(self):
        total = self.current_total({'payment_method': 'check', 'address_id': self.address.pk})
        set_limit(self.user, total - 1)
        before = self.cart_state()
        response = self.place()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(Order.objects.filter(user=self.user).count(), 0)
        self.assertEqual(self.cart_state(), before)
        html = response.content.decode()
        self.assertIn('اعتبار چکی باقی‌مانده', html)
        self.assertIn('مازاد 1 تومان', html)
        self.assertIn('روش پرداخت نقدی', html)

    def test_the_message_shows_amount_limit_used_and_excess(self):
        set_limit(self.user, 250_000)
        make_order(self.user, 100_000)                                          # مصرف‌شده
        response = self.place()                                                 # سفارش ۲۰۰٬۰۰۰ ← مانده ۱۵۰٬۰۰۰
        self.assertEqual(response.status_code, 409)
        html = response.content.decode()
        for text in ('200,000', '150,000', '250,000', '100,000', 'مازاد 50,000'):
            self.assertIn(text, html)

    def test_a_frozen_limit_blocks_cheque_orders_but_not_cash(self):
        set_limit(self.user, 0)
        response = self.place()
        self.assertEqual(response.status_code, 409)
        self.assertIn('فریز', response.content.decode())
        self.assertEqual(Order.objects.count(), 0)
        cash = self.place('cash')                                               # سفارش نقدی/آنلاین سقف ندارد
        self.assertEqual(cash.status_code, 302)

    def test_cash_orders_ignore_the_limit_entirely(self):
        set_limit(self.user, 1)
        self.assertEqual(self.place('cash').status_code, 302)

    def test_outstanding_orders_accumulate(self):
        total = self.current_total({'payment_method': 'check', 'address_id': self.address.pk})
        set_limit(self.user, total * 2 - 1)                                     # جای یک سفارش کامل و کمی کمتر از دومی
        self.assertEqual(self.place().status_code, 302)
        CartItem.objects.create(cart=Cart.objects.create(user=self.user), product=self.product, quantity=2)
        response = self.place()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(Order.objects.filter(user=self.user).count(), 1)

    def test_cancelling_the_first_order_makes_room_again(self):
        total = self.current_total({'payment_method': 'check', 'address_id': self.address.pk})
        set_limit(self.user, total)
        self.assertEqual(self.place().status_code, 302)
        first = Order.objects.get(user=self.user)
        CartItem.objects.create(cart=Cart.objects.create(user=self.user), product=self.product, quantity=2)
        self.assertEqual(self.place().status_code, 409)
        first.status = 'canceled'
        first.save()
        self.assertEqual(self.place().status_code, 302)

    def test_other_users_orders_do_not_use_up_my_credit(self):
        make_order(self.other, 10 ** 9)
        set_limit(self.user, 1_000_000)
        self.assertEqual(self.place().status_code, 302)

    def test_a_cash_customer_with_the_permission_is_limited_on_the_cheque_option(self):
        CustomUser.objects.filter(pk=self.user.pk).update(price_level=2, can_purchase_with_check=True, cheque_credit_limit=1)
        response = self.place('check')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(Order.objects.count(), 0)

    def test_a_vip_cheque_option_is_limited_too(self):
        CustomUser.objects.filter(pk=self.user.pk).update(price_level=3, cheque_credit_limit=1)
        set_policy(VIP_CHEQUE_VIP_PRICE)
        response = self.place('vip_check')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(self.place('vip').status_code, 302)                    # vip آنلاین سقف ندارد

    def test_the_request_option_and_denied_options_are_not_affected(self):
        set_limit(self.user, 0)
        response = self.client.post(reverse('orders:submit_order'), {'address_id': self.address.pk, 'payment_method': 'request_check'})
        self.assertNotEqual(response.status_code, 409)

    def test_the_gate_runs_after_the_price_check_and_never_creates_reservations(self):
        from products.models import StockReservation
        set_limit(self.user, 1)
        self.place()
        self.assertEqual(StockReservation.objects.count(), 0)


# ------------------------------------------------------------------ تأیید درخواست سقف را روی کاربر می‌نویسد
class ApprovalWritesTheLimitTests(CheckoutTestBase):
    def test_an_approved_request_limits_the_cash_customer_at_checkout(self):
        from accounts.tests_cheque_credit import docs, form_data, valid_national_code
        from orders.tests_cheques import make_image, upload
        CustomUser.objects.filter(pk=self.user.pk).update(price_level=2, national_code=valid_national_code())
        user = CustomUser.objects.get(pk=self.user.pk)
        with self.captureOnCommitCallbacks(execute=True):
            request = credit_service.submit_request(user, form_data(requested_limit='150,000'), [('cheque_book', upload(make_image()))])
        admin = make_approved_user('09120000999', is_staff=True, is_superuser=True)
        with self.captureOnCommitCallbacks(execute=True):
            credit_service.approve_request(request, admin)                      # بدون مقدار ← سقف درخواستی
        self.assertEqual(int(CustomUser.objects.get(pk=user.pk).cheque_credit_limit), 150_000)
        with mock.patch('holoo.receivers.send_order_to_holoo'):
            response = self.post_order({'address_id': self.address.pk, 'payment_method': 'check'})   # ۲۰۰٬۰۰۰ > ۱۵۰٬۰۰۰
        self.assertEqual(response.status_code, 409)


# ------------------------------------------------------------------ مایگریشن داده
class DataMigrationTests(TestCase):
    migration = import_module('accounts.migrations.0019_copy_approved_credit_limits')

    def make_request(self, user, status='approved', limit=1000, decided_days_ago=0):
        return ChequeCreditRequest.objects.create(
            user=user, first_name='ع', last_name='ر', national_code='1234567890', business_name='ف', bank_name='ملی',
            account_holder='ع', iban='IR050170000000123456789012', requested_limit=5000, status=status, approved_limit=limit,
            rejection_reason='x' if status == 'rejected' else '', decided_at=timezone.now() - timedelta(days=decided_days_ago))

    def run_migration(self):
        from django.apps import apps
        self.migration.copy_limits(apps, None)

    def user(self, phone, flag=True):
        user = make_approved_user(phone, price_level=2)
        CustomUser.objects.filter(pk=user.pk).update(can_purchase_with_check=flag)
        return user

    def limit(self, user):
        return CustomUser.objects.get(pk=user.pk).cheque_credit_limit

    def test_the_latest_approved_limit_is_copied(self):
        user = self.user('09120000601')
        self.make_request(user, limit=1000, decided_days_ago=10)
        self.make_request(user, limit=2500, decided_days_ago=1)
        self.run_migration()
        self.assertEqual(self.limit(user), 2500)

    def test_users_without_the_permission_or_without_a_limit_stay_unlimited(self):
        revoked = self.user('09120000602', flag=False)
        self.make_request(revoked, limit=1000)
        no_limit = self.user('09120000603')
        self.make_request(no_limit, limit=None)
        rejected = self.user('09120000604')
        self.make_request(rejected, status='rejected', limit=None)
        plain = self.user('09120000605', flag=False)                              # مثل مشتری چکیِ سطح ۱: هیچ درخواستی ندارد
        self.run_migration()
        for user in (revoked, no_limit, rejected, plain):
            self.assertIsNone(self.limit(user))

    def test_it_is_idempotent(self):
        user = self.user('09120000606')
        self.make_request(user, limit=777)
        self.run_migration()
        self.run_migration()
        self.assertEqual(self.limit(user), 777)


# ------------------------------------------------------------------ فیلد و ادمین
class FieldAndAdminTests(TestCase):
    def setUp(self):
        self.user = make_approved_user('09120000611', price_level=2)
        self.admin = make_approved_user('09120000612', is_staff=True, is_superuser=True)
        self.client.force_login(self.admin)

    def test_null_is_the_default(self):
        self.assertIsNone(CustomUser.objects.get(pk=self.user.pk).cheque_credit_limit)

    def test_negative_values_are_refused_by_validation_and_by_the_database(self):
        self.user.cheque_credit_limit = -5
        with self.assertRaises(ValidationError):
            self.user.full_clean(exclude=[f.name for f in self.user._meta.fields if f.name != 'cheque_credit_limit'])
        with self.assertRaises(IntegrityError), transaction.atomic():
            CustomUser.objects.filter(pk=self.user.pk).update(cheque_credit_limit=-5)

    def test_zero_and_positive_values_are_valid(self):
        for value in (0, 1, 10 ** 12):
            self.user.cheque_credit_limit = value
            self.user.full_clean(exclude=[f.name for f in self.user._meta.fields if f.name != 'cheque_credit_limit'])

    def test_the_user_admin_shows_the_limit_and_the_usage(self):
        set_limit(self.user, 5_000_000)
        make_order(self.user, 1_200_000)
        page = self.client.get(reverse('admin:accounts_customuser_change', args=[self.user.pk]))
        self.assertContains(page, 'name="cheque_credit_limit"')
        self.assertContains(page, 'مصرف‌شده 1,200,000 از 5,000,000 تومان')
        self.assertContains(page, 'باقی‌مانده 3,800,000')

    def test_the_usage_line_for_unlimited_and_frozen(self):
        page = self.client.get(reverse('admin:accounts_customuser_change', args=[self.user.pk]))
        self.assertContains(page, 'بدون سقف')
        set_limit(self.user, 0)
        self.assertContains(self.client.get(reverse('admin:accounts_customuser_change', args=[self.user.pk])), 'اعتبار فریز است')

    def test_the_admin_can_clear_the_limit_to_make_it_unlimited(self):
        set_limit(self.user, 1000)
        admin_class = __import__('accounts.admin', fromlist=['CustomUserAdmin']).CustomUserAdmin
        self.assertIn('cheque_credit_limit', [f for _, o in admin_class.fieldsets for f in o['fields']])
        CustomUser.objects.filter(pk=self.user.pk).update(cheque_credit_limit=None)
        self.assertIsNone(CustomUser.objects.get(pk=self.user.pk).cheque_credit_limit)


# ------------------------------------------------------------------ هم‌زمانی
class ConcurrencyTests(TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        from locations.models import City, Province
        from products.models import Category, Product, SiteSettings
        from django.core.cache import cache
        self.addCleanup(cache.delete, SiteSettings.CACHE_KEY)
        SiteSettings.load()
        province = Province.objects.create(name='استان سقف')
        self.city = City.objects.create(province=province, name='شهر سقف')
        category = Category.objects.create(name='سقف', slug='credit-race-cat')
        self.product = Product.objects.create(name='کالای سقف', slug='credit-race-p', erp_code='ERP-CRACE', category=category,
                                              price=100000, price2=90000, stock=100)

    def buyer(self, index, limit):
        user = make_approved_user(f'0912077{index:04d}', price_level=1)
        CustomUser.objects.filter(pk=user.pk).update(cheque_credit_limit=limit)
        cart = Cart.objects.create(user=user)
        CartItem.objects.create(cart=cart, product=self.product, quantity=1)
        address = Address.objects.create(user=user, title='خانه', receiver_first_name='الف', receiver_last_name='ب',
                                         receiver_phone='09123334455', city=self.city, postal_code='1112223334', address='خیابان سقف')
        return user, address

    def post_job(self, user, address):
        def job():
            client = Client()
            client.force_login(user)
            return client.post(reverse('orders:submit_order'), {'address_id': address.pk, 'payment_method': 'check', 'expected_total': 100000})
        return job

    def run_threads(self, jobs):
        barrier = threading.Barrier(len(jobs))
        results = [None] * len(jobs)

        def worker(index, job):
            try:
                barrier.wait(timeout=30)
                results[index] = job()
            except BaseException as error:                               # noqa: BLE001
                results[index] = error
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=(i, job)) for i, job in enumerate(jobs)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        self.assertFalse(any(t.is_alive() for t in threads), 'یک نخ گیر کرد (احتمال deadlock)')
        return results

    def test_many_users_each_at_their_limit_all_succeed_without_deadlock(self):
        buyers = [self.buyer(i, limit=100000) for i in range(6)]
        with mock.patch('holoo.receivers.send_order_to_holoo'):
            results = self.run_threads([self.post_job(u, a) for u, a in buyers])
        self.assertEqual([r for r in results if isinstance(r, BaseException)], [])
        self.assertEqual(sorted(r.status_code for r in results), [302] * 6)

    def test_users_over_their_limit_are_all_refused_in_parallel(self):
        buyers = [self.buyer(i, limit=99999) for i in range(6)]
        with mock.patch('holoo.receivers.send_order_to_holoo'):
            results = self.run_threads([self.post_job(u, a) for u, a in buyers])
        self.assertEqual([r for r in results if isinstance(r, BaseException)], [])
        self.assertEqual(sorted(r.status_code for r in results), [409] * 6)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(Cart.objects.count(), 6)                                 # سبدها دست‌نخورده

    def test_a_double_submit_by_one_user_never_exceeds_the_limit(self):
        user, address = self.buyer(0, limit=100000)
        with mock.patch('holoo.receivers.send_order_to_holoo'):
            results = self.run_threads([self.post_job(user, address), self.post_job(user, address)])
        self.assertEqual([r for r in results if isinstance(r, BaseException)], [])
        self.assertEqual(Order.objects.filter(user=user).count(), 1)
        self.assertLessEqual(credit.outstanding_total(user), 100000)

    def test_checkout_waits_for_a_concurrent_order_of_the_same_user_and_sees_it(self):
        """ سفارشی که هم‌زمان (مسیر دیگر) برای همین کاربر زیر قفل ردیف کاربر ساخته می‌شود، پیش از تصمیم گیت دیده می‌شود """
        user, address = self.buyer(1, limit=150000)
        holding, release = threading.Event(), threading.Event()

        def other_path():
            try:
                with transaction.atomic():
                    CustomUser.objects.select_for_update().get(pk=user.pk)
                    holding.set()
                    release.wait(timeout=30)
                    make_order(user, 100000)                                      # سفارش دیگری که هنوز commit نشده
                return 'done'
            finally:
                connection.close()

        def checkout():
            try:
                holding.wait(timeout=30)
                client = Client()
                client.force_login(user)
                timer = threading.Timer(1.0, release.set)                         # بعد از شروع انتظار قفل، سفارشِ دیگر commit می‌شود
                timer.start()
                with mock.patch('holoo.receivers.send_order_to_holoo'):
                    return client.post(reverse('orders:submit_order'), {'address_id': address.pk, 'payment_method': 'check',
                                                                         'expected_total': 100000})
            finally:
                connection.close()

        results = [None, None]
        threads = [threading.Thread(target=lambda: results.__setitem__(0, other_path())),
                   threading.Thread(target=lambda: results.__setitem__(1, checkout()))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(results[0], 'done')
        self.assertEqual(results[1].status_code, 409)                             # ۱۰۰٬۰۰۰ مصرف‌شده + ۱۰۰٬۰۰۰ جدید > ۱۵۰٬۰۰۰
        self.assertEqual(Order.objects.filter(user=user).count(), 1)
        self.assertEqual(Cart.objects.filter(user=user).count(), 1)
