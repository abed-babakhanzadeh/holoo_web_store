"""
تست‌های Loyalty Phase 2B: موتور کسب خودکار امتیاز (loyalty/earning.py + loyalty/receivers.py).

هم‌الگوی accounts/tests_loyalty.py برای مدیریت کش SiteSettings: هر تست کش تنظیمات را قبل/بعد
پاک می‌کند و تغییرات را با save() واقعی (نه queryset.update()) انجام می‌دهد تا سیگنال ابطال
کش شلیک شود (SiteSettings.cached() از کش مشترک می‌خواند که rollback تراکنش TestCase به آن
سیگنال نمی‌فرستد).
"""

import itertools
import threading
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomUser
from orders.models import Order, OrderItem
from payments import checkout
from payments.models import Transaction
from products.models import SiteSettings

from . import earning, services
from .exceptions import IdempotencyKeyConflictError
from .models import LoyaltyAccount, LoyaltyTransaction

_seq = itertools.count(1)


def _make_user():
    return CustomUser.objects.create_user(phone_number=f'0912080{next(_seq):04d}')


def _make_order(user, *, item_price=None, item_qty=1, shipping_cost=0, status='delivered'):
    """ سفارش با اقلام واقعی (اگر item_price داده شود) - هم‌الگوی accounts/tests_loyalty.py::_make_paid_order """
    items_total = (item_price or 0) * item_qty
    order = Order.objects.create(
        user=user, first_name='کاربر', last_name='تست', phone=user.phone_number if user else '0912000000',
        address='تهران', payment_method='cash', shipping_cost=shipping_cost,
        total_price=items_total + shipping_cost, status=status,
    )
    if item_price is not None:
        OrderItem.objects.create(order=order, price=item_price, quantity=item_qty)
    return order


def _make_transaction(order, *, status='success', authority=None):
    return Transaction.objects.create(
        user=order.user, order=order, amount=order.total_price,
        authority=authority or f'TEST-EARN-{next(_seq)}', status=status,
    )


class LoyaltyEarningTestBase(TestCase):
    def setUp(self):
        super().setUp()
        cache.delete(SiteSettings.CACHE_KEY)
        SiteSettings.load().save()
        self.addCleanup(self._reset_settings)

    def _reset_settings(self):
        obj = SiteSettings.load()
        obj.loyalty_mode = SiteSettings.LOYALTY_MODE_ORDER_COUNT
        obj.loyalty_points_per_order = 100
        obj.loyalty_amount_step = 100000
        obj.loyalty_activated_at = None
        obj.save()
        cache.delete(SiteSettings.CACHE_KEY)

    def activate_before(self, moment):
        """ فعال‌سازی باشگاه دقیقاً قبل از moment (یعنی سفارش‌های در/بعد از moment واجد شرایط‌اند) """
        settings_obj = SiteSettings.load()
        settings_obj.loyalty_activated_at = moment - timedelta(minutes=1)
        settings_obj.save()
        return SiteSettings.cached()

    def activate_after(self, moment):
        """ فعال‌سازی باشگاه بعد از moment (یعنی سفارش‌های ثبت‌شده در moment هنوز واجد شرایط نیستند) """
        settings_obj = SiteSettings.load()
        settings_obj.loyalty_activated_at = moment + timedelta(minutes=1)
        settings_obj.save()
        return SiteSettings.cached()


# ============================================================================== فرمول کسب امتیاز
class EarnFormulaTests(LoyaltyEarningTestBase):
    def test_order_count_mode_gives_flat_points_per_order_regardless_of_amount(self):
        settings_obj = SiteSettings.cached()
        user = _make_user()
        order = _make_order(user, item_price=999_999_999, item_qty=3, shipping_cost=50000)
        self.assertEqual(earning.calculate_order_earn_points(order, settings_obj), 100)

    def test_amount_mode_uses_net_items_total_floor_divided_by_step(self):
        settings_obj = self.activate_before(timezone.now())
        settings_obj.loyalty_mode = SiteSettings.LOYALTY_MODE_AMOUNT
        settings_obj.loyalty_amount_step = 100000
        settings_obj.save()
        settings_obj = SiteSettings.cached()

        user = _make_user()
        order = _make_order(user, item_price=250000, item_qty=1)   # ۲٬۵۰۰٬۰۰ // ۱۰۰٬۰۰۰ = ۲
        self.assertEqual(earning.calculate_order_earn_points(order, settings_obj), 2)

    def test_shipping_cost_is_never_part_of_the_amount(self):
        settings_obj = SiteSettings.load()
        settings_obj.loyalty_mode = SiteSettings.LOYALTY_MODE_AMOUNT
        settings_obj.loyalty_amount_step = 100000
        settings_obj.save()
        settings_obj = SiteSettings.cached()

        user = _make_user()
        order = _make_order(user, item_price=100000, item_qty=1, shipping_cost=999_999_999)
        self.assertEqual(earning.calculate_order_earn_points(order, settings_obj), 1)   # فقط ۱۰۰٬۰۰۰ اقلام

    def test_order_level_coupon_discount_does_not_reduce_the_amount(self):
        """
        order_discount (تخفیف کد سفارش سطح سفارش) هرگز در items_total کم نمی‌شود - چون در هیچ
        OrderItem.price ذخیره نشده (فقط یک عدد کلی روی خودِ Order)؛ دقیقاً هم‌رفتار
        orders/stats.py:orders_paid_net_amount که همین فرمول را برای Tier زنده استفاده می‌کند.
        """
        settings_obj = SiteSettings.load()
        settings_obj.loyalty_mode = SiteSettings.LOYALTY_MODE_AMOUNT
        settings_obj.loyalty_amount_step = 100000
        settings_obj.save()
        settings_obj = SiteSettings.cached()

        user = _make_user()
        order = _make_order(user, item_price=500000, item_qty=1)
        order.order_discount = Decimal('200000')   # اگر روی items_total اثر می‌گذاشت، امتیاز باید ۳ می‌شد نه ۵
        order.save()
        self.assertEqual(earning.calculate_order_earn_points(order, settings_obj), 5)


# ============================================================================== شرایط احراز
class EligibilityTests(LoyaltyEarningTestBase):
    def test_failed_transaction_earns_nothing(self):
        self.activate_before(timezone.now())
        user = _make_user()
        order = _make_order(user, item_price=100000)
        txn = _make_transaction(order, status='failed')
        self.assertIsNone(earning.earn_from_payment(order, txn))
        self.assertFalse(LoyaltyTransaction.objects.exists())

    def test_pending_transaction_earns_nothing(self):
        self.activate_before(timezone.now())
        user = _make_user()
        order = _make_order(user, item_price=100000)
        txn = _make_transaction(order, status='pending')
        self.assertIsNone(earning.earn_from_payment(order, txn))
        self.assertFalse(LoyaltyTransaction.objects.exists())

    def test_guest_or_deleted_user_order_earns_nothing(self):
        self.activate_before(timezone.now())
        order = Order.objects.create(
            user=None, first_name='مهمان', last_name='تست', phone='09120000001',
            address='تهران', payment_method='cash', total_price=100000, status='delivered',
        )
        OrderItem.objects.create(order=order, price=100000, quantity=1)
        holder = _make_user()   # فقط برای FK غیرنال Transaction.user لازم است
        txn = Transaction.objects.create(
            user=holder, order=order, amount=order.total_price, authority='TEST-GUEST', status='success',
        )
        self.assertIsNone(earning.earn_from_payment(order, txn))
        self.assertFalse(LoyaltyTransaction.objects.exists())

    def test_canceled_order_earns_nothing(self):
        self.activate_before(timezone.now())
        user = _make_user()
        order = _make_order(user, item_price=100000, status='canceled')
        txn = _make_transaction(order)
        self.assertIsNone(earning.earn_from_payment(order, txn))
        self.assertFalse(LoyaltyTransaction.objects.exists())

    def test_null_activation_boundary_earns_nothing(self):
        """ پیش‌فرض پس از فاز ۲A: loyalty_activated_at=None یعنی باشگاه هنوز فعال نشده """
        user = _make_user()
        order = _make_order(user, item_price=100000)
        txn = _make_transaction(order)
        self.assertIsNone(SiteSettings.cached().loyalty_activated_at)
        self.assertIsNone(earning.earn_from_payment(order, txn))
        self.assertFalse(LoyaltyTransaction.objects.exists())

    def test_order_created_before_activation_boundary_earns_nothing(self):
        user = _make_user()
        order = _make_order(user, item_price=100000)
        self.activate_after(order.created_at)   # مرز فعال‌سازی بعد از این سفارش
        txn = _make_transaction(order)
        self.assertIsNone(earning.earn_from_payment(order, txn))
        self.assertFalse(LoyaltyTransaction.objects.exists())

    def test_order_created_after_activation_boundary_earns_normally(self):
        user = _make_user()
        order = _make_order(user, item_price=100000)
        self.activate_before(order.created_at)   # مرز فعال‌سازی قبل از این سفارش
        txn = _make_transaction(order)
        result = earning.earn_from_payment(order, txn)
        self.assertIsNotNone(result)
        self.assertEqual(result.amount, 100)

    def test_zero_computed_points_creates_no_ledger_row(self):
        settings_obj = SiteSettings.load()
        settings_obj.loyalty_mode = SiteSettings.LOYALTY_MODE_AMOUNT
        settings_obj.loyalty_amount_step = 1_000_000
        settings_obj.save()

        user = _make_user()
        order = _make_order(user, item_price=100000, item_qty=1)   # ۱۰۰٬۰۰۰ // ۱٬۰۰۰٬۰۰۰ = ۰
        self.activate_before(order.created_at)
        txn = _make_transaction(order)
        self.assertIsNone(earning.earn_from_payment(order, txn))
        self.assertFalse(LoyaltyTransaction.objects.exists())
        self.assertFalse(LoyaltyAccount.objects.filter(user=user).exists())


# ============================================================================== موجودی/بالانس
class BalanceEffectTests(LoyaltyEarningTestBase):
    def test_successful_earn_increases_balance_and_lifetime_earned_only(self):
        user = _make_user()
        order = _make_order(user, item_price=100000)
        self.activate_before(order.created_at)
        txn = _make_transaction(order)

        earning.earn_from_payment(order, txn)

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 100)
        self.assertEqual(account.lifetime_earned, 100)
        self.assertEqual(account.lifetime_redeemed, 0)

    def test_ledger_row_has_correct_type_and_source(self):
        user = _make_user()
        order = _make_order(user, item_price=100000)
        self.activate_before(order.created_at)
        txn = _make_transaction(order)

        result = earning.earn_from_payment(order, txn)
        self.assertEqual(result.transaction_type, LoyaltyTransaction.EARN_ORDER)
        self.assertEqual(result.source_type, 'order')
        self.assertEqual(result.source_id, order.id)
        self.assertEqual(result.idempotency_key, f'loyalty-earn-order-{order.id}')


# ============================================================================== ایدمپوتنسی
class IdempotencyTests(LoyaltyEarningTestBase):
    def test_processing_the_same_transaction_twice_earns_only_once(self):
        """ کال‌بک تکراری/رفرش صفحه - همان (order, transaction) دوبار پردازش می‌شود """
        user = _make_user()
        order = _make_order(user, item_price=100000)
        self.activate_before(order.created_at)
        txn = _make_transaction(order)

        first = earning.earn_from_payment(order, txn)
        second = earning.earn_from_payment(order, txn)

        self.assertEqual(first.pk, second.pk)
        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 100)
        self.assertEqual(LoyaltyTransaction.objects.filter(source_type='order', source_id=order.id).count(), 1)

    def test_two_successful_transactions_for_the_same_order_earn_only_once(self):
        """
        سناریوی نظری «چند تراکنش پرداختی موفق برای یک سفارش» - کلید سطح سفارش است، نه سطح
        تراکنش، پس دومین Transaction موفق هیچ Earn تازه‌ای نمی‌سازد.
        """
        user = _make_user()
        order = _make_order(user, item_price=100000)
        self.activate_before(order.created_at)
        first_txn = _make_transaction(order, authority='TEST-EARN-A')
        second_txn = _make_transaction(order, authority='TEST-EARN-B')

        first = earning.earn_from_payment(order, first_txn)
        second = earning.earn_from_payment(order, second_txn)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(LoyaltyAccount.objects.get(user=user).current_balance, 100)

    def test_conflicting_amount_for_the_same_order_key_raises_instead_of_silently_returning(self):
        """
        اثبات اینکه رفتار فاز ۱ (IdempotencyKeyConflictError برای همان کلید با amount متفاوت)
        در مسیر Earn هم دست‌نخورده مانده - نه بازنویسی و نه Silent-Override.
        """
        user = _make_user()
        order = _make_order(user, item_price=100000)
        self.activate_before(order.created_at)
        txn = _make_transaction(order)
        earning.earn_from_payment(order, txn)   # اولین Earn واقعی: ۱۰۰ امتیاز

        with self.assertRaises(IdempotencyKeyConflictError):
            services.credit_points(
                user, 999, LoyaltyTransaction.EARN_ORDER, 'دستکاری فرضی',
                idempotency_key=f'loyalty-earn-order-{order.id}',
            )


# ============================================================================== عدم رگرسیون Tier زنده
class LiveTierRegressionTests(LoyaltyEarningTestBase):
    def test_get_loyalty_points_and_level_are_unaffected_by_the_ledger(self):
        """
        CustomUser.get_loyalty_points/get_loyalty_level (accounts/models.py) هرگز از لجر
        loyalty نمی‌خوانند - حتی بعد از Earn واقعی، مقدارشان دقیقاً همان فرمول قدیمی
        (paid_orders_count × loyalty_points_per_order) است، نه چیزی مرتبط با LoyaltyAccount.
        """
        user = _make_user()
        order = _make_order(user, item_price=999999)
        self.activate_before(order.created_at)
        txn = _make_transaction(order)
        earning.earn_from_payment(order, txn)

        fresh_user = CustomUser.objects.get(pk=user.pk)
        self.assertEqual(fresh_user.get_loyalty_points(), 100)   # ۱ سفارش پرداخت‌شده × ۱۰۰ (فرمول قدیمی، بدون تغییر)
        self.assertEqual(fresh_user.get_loyalty_level()[0], 'مشتری جدید')
        self.assertEqual(fresh_user.get_loyalty_level_index(), 0)


# ============================================================================== مسیر واقعی پرداخت (اتصال واقعی سیگنال)
class RealPaymentFlowEarnTests(LoyaltyEarningTestBase):
    """ اثبات اینکه ریسیور واقعاً روی payment_succeeded سوار است - نه فقط فراخوانی مستقیم earning.py """

    def _prepare(self, item_price=100000):
        # status='pending' چون PaymentStartView._get_order فقط سفارش‌های can_pay (pending/registered
        # و بدون پرداخت موفق قبلی) را می‌پذیرد - نگاه کنید orders/models.py:Order.can_pay
        user = _make_user()
        order = _make_order(user, item_price=item_price, status='pending')
        self.activate_before(order.created_at)
        return user, order

    def test_wallet_only_payment_earns_points(self):
        from wallet.models import Wallet
        user, order = self._prepare()
        Wallet.objects.create(user=user, balance=200000)

        with mock.patch('holoo.receivers.confirm_payment_in_holoo'):
            with self.captureOnCommitCallbacks(execute=True):
                checkout.start_order_payment(order, user, Decimal('100000'))

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 100)

    def test_gateway_only_payment_earns_points_on_successful_callback(self):
        user, order = self._prepare()
        self.client.force_login(user)

        with mock.patch('holoo.receivers.confirm_payment_in_holoo'):
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(reverse('payments:start_payment', args=[order.id]), {'wallet_amount': '0'})
                txn = Transaction.objects.get(order=order)
                self.client.get(reverse('payments:callback'), {'Authority': txn.authority, 'Status': 'OK'})

        self.assertEqual(LoyaltyAccount.objects.get(user=user).current_balance, 100)

    def test_failed_gateway_callback_earns_nothing(self):
        user, order = self._prepare()
        self.client.force_login(user)

        with mock.patch('holoo.receivers.confirm_payment_in_holoo'):
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(reverse('payments:start_payment', args=[order.id]), {'wallet_amount': '0'})
                txn = Transaction.objects.get(order=order)
                self.client.get(reverse('payments:callback'), {'Authority': txn.authority, 'Status': 'FAIL'})

        self.assertFalse(LoyaltyTransaction.objects.exists())

    def test_mixed_payment_earns_points_based_on_full_order_not_just_gateway_share(self):
        from wallet.models import Wallet
        user, order = self._prepare(item_price=300000)
        Wallet.objects.create(user=user, balance=100000)
        self.client.force_login(user)

        with mock.patch('holoo.receivers.confirm_payment_in_holoo'):
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(reverse('payments:start_payment', args=[order.id]), {'wallet_amount': '100000'})
                txn = Transaction.objects.filter(order=order, status='pending').get()
                self.client.get(reverse('payments:callback'), {'Authority': txn.authority, 'Status': 'OK'})

        # امتیاز بر مبنای order.items_total (کل سفارش) است، نه فقط سهم درگاه
        self.assertEqual(LoyaltyAccount.objects.get(user=user).current_balance, 100)


# ============================================================================== هم‌زمانی واقعی
class EarnConcurrencyTests(TransactionTestCase):
    """
    دو تلاش هم‌زمانِ واقعی (نخ/اتصال دیتابیس جدا) برای Earn روی همان سفارش - هم‌الگوی
    loyalty/tests.py::ConcurrencyTests و orders/tests_coupon_concurrency.py.
    """

    # قرارداد پروژه (cart/tests.py): همه‌ی TransactionTestCaseها serialized_rollback=True؛ وگرنه flushِ یکی (که post_migrate را دوباره
    # اجرا می‌کند و مثلاً گروه «کارشناس پشتیبانی» چت را می‌سازد) بازیابیِ سریال‌شده‌ی کلاس بعدی را می‌شکند
    serialized_rollback = True

    def setUp(self):
        self.addCleanup(cache.delete, SiteSettings.CACHE_KEY)

    def run_threads(self, jobs):
        barrier = threading.Barrier(len(jobs))
        results = [None] * len(jobs)

        def worker(index, job):
            try:
                barrier.wait(timeout=30)
                results[index] = job()
            except BaseException as error:   # noqa: BLE001
                results[index] = error
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=(i, job)) for i, job in enumerate(jobs)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertFalse(any(t.is_alive() for t in threads), 'یک نخ گیر کرد (احتمال deadlock)')
        return results

    def test_two_concurrent_earn_attempts_on_the_same_order_never_double_credit(self):
        cache.delete(SiteSettings.CACHE_KEY)
        settings_obj = SiteSettings.load()
        settings_obj.loyalty_activated_at = timezone.now() - timedelta(days=1)
        settings_obj.save()

        user = CustomUser.objects.create_user(phone_number='09120809999')
        order = Order.objects.create(
            user=user, first_name='کاربر', last_name='تست', phone=user.phone_number,
            address='تهران', payment_method='cash', total_price=100000, status='delivered',
        )
        OrderItem.objects.create(order=order, price=100000, quantity=1)
        txn = Transaction.objects.create(
            user=user, order=order, amount=order.total_price, authority='TEST-EARN-RACE', status='success',
        )

        jobs = [(lambda: earning.earn_from_payment(order, txn)) for _ in range(8)]
        results = self.run_threads(jobs)

        unexpected = [r for r in results if not isinstance(r, LoyaltyTransaction)]
        self.assertEqual(unexpected, [], f'نتایج غیرمنتظره: {unexpected}')

        pks = {r.pk for r in results}
        self.assertEqual(len(pks), 1)   # همه به یک رکورد واحد اشاره می‌کنند
        self.assertEqual(LoyaltyAccount.objects.get(user=user).current_balance, 100)   # نه ۸۰۰

        settings_obj.loyalty_activated_at = None
        settings_obj.save()
