"""
تست‌های Loyalty Phase 2C: بازگشت امتیاز از لغو سفارش
(loyalty/cancellation.py + loyalty/receivers.py::on_order_canceled).

هم‌الگوی loyalty/tests_earning.py برای مدیریت کش SiteSettings.
"""

import itertools
import threading
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from decimal import Decimal

from accounts.models import CustomUser
from orders.models import Order, OrderItem
from payments.models import Transaction
from products.models import SiteSettings
from returns.models import ReturnItem, ReturnReason, ReturnRequest

from . import cancellation, earning, services
from .exceptions import InsufficientPointsError
from .models import LoyaltyAccount, LoyaltyTransaction
from .returns import reverse_from_return

_seq = itertools.count(1)


def _make_user():
    return CustomUser.objects.create_user(phone_number=f'0912081{next(_seq):04d}')


def _make_order(user, *, item_price, item_qty=1, status='pending'):
    total = item_price * item_qty
    order = Order.objects.create(
        user=user, first_name='کاربر', last_name='تست', phone=user.phone_number,
        address='تهران', payment_method='cash', total_price=total, status=status,
    )
    OrderItem.objects.create(order=order, price=item_price, quantity=item_qty)
    return order


def _pay(order, *, authority=None):
    return Transaction.objects.create(
        user=order.user, order=order, amount=order.total_price,
        authority=authority or f'TEST-CANCEL-{next(_seq)}', status='success',
    )


class LoyaltyCancellationTestBase(TestCase):
    def setUp(self):
        super().setUp()
        cache.delete(SiteSettings.CACHE_KEY)
        SiteSettings.load().save()
        self.addCleanup(self._reset_settings)

    def _reset_settings(self):
        obj = SiteSettings.load()
        obj.loyalty_mode = SiteSettings.LOYALTY_MODE_ORDER_COUNT
        obj.loyalty_points_per_order = 100
        obj.loyalty_activated_at = None
        obj.save()
        cache.delete(SiteSettings.CACHE_KEY)

    def activate_before(self, moment):
        settings_obj = SiteSettings.load()
        settings_obj.loyalty_activated_at = moment - timedelta(minutes=1)
        settings_obj.save()
        return SiteSettings.cached()

    def make_earning_order(self, *, item_price=100000, points_per_order=100):
        """ سفارش پرداخت‌شده‌ی واجد شرایط، با یک EARN_ORDER واقعی از قبل ثبت‌شده. """
        settings_obj = SiteSettings.load()
        settings_obj.loyalty_mode = SiteSettings.LOYALTY_MODE_ORDER_COUNT
        settings_obj.loyalty_points_per_order = points_per_order
        settings_obj.save()

        user = _make_user()
        order = _make_order(user, item_price=item_price)
        self.activate_before(order.created_at)
        txn = _pay(order)
        earn_txn = earning.earn_from_payment(order, txn)
        self.assertIsNotNone(earn_txn, 'پیش‌شرط تست: کسب امتیاز باید موفق شده باشد')
        return user, order


# ============================================================================== منطق اصلی بازگشت
class ReverseFromCancellationTests(LoyaltyCancellationTestBase):
    def test_canceling_order_with_earn_creates_reverse_and_reduces_balance(self):
        user, order = self.make_earning_order()
        self.assertEqual(LoyaltyAccount.objects.get(user=user).current_balance, 100)

        result = cancellation.reverse_from_cancellation(order)

        self.assertIsNotNone(result)
        self.assertEqual(result.transaction_type, LoyaltyTransaction.REVERSE)
        self.assertEqual(result.amount, -100)
        self.assertEqual(result.source_type, 'order')
        self.assertEqual(result.source_id, order.id)
        self.assertEqual(result.idempotency_key, f'loyalty-reverse-cancel-order-{order.id}')

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 0)
        self.assertEqual(account.lifetime_redeemed, 100)

    def test_canceling_order_without_any_earn_is_a_silent_noop(self):
        """ سفارشی که هرگز پرداخت موفق نداشته (پس هرگز EARN_ORDER نگرفته) """
        self.activate_before(timezone.now())
        user = _make_user()
        order = _make_order(user, item_price=100000)   # بدون _pay/بدون earn_from_payment

        result = cancellation.reverse_from_cancellation(order)

        self.assertIsNone(result)
        self.assertFalse(LoyaltyTransaction.objects.exists())

    def test_canceling_order_created_before_activation_boundary_is_noop(self):
        """ سفارش پیش از مرز فعال‌سازی: earn_from_payment از قبل هیچ EARN_ORDER ای نساخته بود """
        user = _make_user()
        order = _make_order(user, item_price=100000)
        # مرز فعال‌سازی *بعد* از این سفارش گذاشته می‌شود - یعنی سفارش واجد شرایط کسب نبود
        settings_obj = SiteSettings.load()
        settings_obj.loyalty_activated_at = order.created_at + timedelta(minutes=1)
        settings_obj.save()

        txn = _pay(order)
        self.assertIsNone(earning.earn_from_payment(order, txn))   # پیش‌شرط: واقعاً earn نگرفته

        result = cancellation.reverse_from_cancellation(order)

        self.assertIsNone(result)
        self.assertFalse(LoyaltyTransaction.objects.exists())

    def test_lifetime_earned_is_never_reduced_by_cancellation_reverse(self):
        user, order = self.make_earning_order()
        cancellation.reverse_from_cancellation(order)

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.lifetime_earned, 100)   # دست‌نخورده (مدل الف - مصوبه‌ی معماری)
        self.assertEqual(account.lifetime_redeemed, 100)

    def test_duplicate_cancellation_call_reverses_only_once(self):
        """ شلیک مجدد سیگنال (مثلاً دو نخ/دو درخواست ادمین) - مستقیماً روی تابع هسته آزموده می‌شود. """
        user, order = self.make_earning_order()

        first = cancellation.reverse_from_cancellation(order)
        second = cancellation.reverse_from_cancellation(order)   # فراخوانی دوم: remaining<=0 → No-Op فوری

        self.assertIsNotNone(first)
        self.assertIsNone(second)   # دومی حتی به debit_points هم نمی‌رسد
        self.assertEqual(
            LoyaltyTransaction.objects.filter(
                source_type='order', source_id=order.id, transaction_type=LoyaltyTransaction.REVERSE,
            ).count(),
            1,
        )
        self.assertEqual(LoyaltyAccount.objects.get(user=user).current_balance, 0)

    def test_duplicate_signal_fired_directly_twice_reverses_only_once(self):
        """ همان سناریوی بالا، این‌بار با شلیک واقعی سیگنال order_canceled دو بار پشت‌سرهم. """
        from orders.models import Order as OrderModel
        from orders.signals import order_canceled

        user, order = self.make_earning_order()

        order_canceled.send_robust(sender=OrderModel, order=order)
        order_canceled.send_robust(sender=OrderModel, order=order)

        self.assertEqual(
            LoyaltyTransaction.objects.filter(
                source_type='order', source_id=order.id, transaction_type=LoyaltyTransaction.REVERSE,
            ).count(),
            1,
        )
        self.assertEqual(LoyaltyAccount.objects.get(user=user).current_balance, 0)

    def test_only_the_unreversed_remainder_is_taken_back(self):
        """
        اگر بخشی از امتیاز این سفارش قبلاً (با هر مکانیزم دیگری) به همان source برگشته باشد،
        بازگشتِ لغو فقط باقی‌مانده‌ی برنگشته را می‌گیرد - نه کل مقدار اولیه‌ی کسب را دوباره.
        """
        user, order = self.make_earning_order()
        # وانمود می‌کنیم بخشی از این سفارش قبلاً (مثلاً با یک فرآیند مستقل) برگشته
        services.debit_points(
            user, 40, LoyaltyTransaction.REVERSE, 'برگشت جزئی فرضی',
            source_type='order', source_id=order.id, idempotency_key=f'manual-partial-reverse-{order.id}',
        )

        result = cancellation.reverse_from_cancellation(order)

        self.assertEqual(result.amount, -60)   # ۱۰۰ - ۴۰ قبلی
        self.assertEqual(LoyaltyAccount.objects.get(user=user).current_balance, 0)


# ============================================================================== موجودی کافی نیست
class InsufficientBalanceTests(LoyaltyCancellationTestBase):
    def test_insufficient_balance_raises_the_domain_error(self):
        """ اثبات مستقیم رفتار services.debit_points از فاز ۱، بدون واسطه‌ی ریسیور. """
        user, order = self.make_earning_order()
        # نیمی از امتیاز را جایی دیگر (مثلاً کسر دستی ادمین) از قبل خرج/کسر شده فرض می‌کنیم
        services.debit_points(user, 50, LoyaltyTransaction.ADMIN_DEBIT, 'کسر دستی نمونه')

        with self.assertRaises(InsufficientPointsError):
            cancellation.reverse_from_cancellation(order)

        # atomic بودن debit_points: هیچ رکورد ناقصی نمانده و موجودی دست‌نخورده است
        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 50)
        self.assertFalse(LoyaltyTransaction.objects.filter(transaction_type=LoyaltyTransaction.REVERSE).exists())

    def test_receiver_catches_and_logs_without_crashing_the_cancellation_cycle(self):
        """
        همان سناریو، اما از مسیر واقعی سیگنال order_canceled و ریسیور loyalty/receivers.py -
        نه فقط خطا نباید بترکد، بلکه باید صریحاً لاگ شود (نه بی‌صدا بلعیده شود).
        """
        from orders.models import Order as OrderModel
        from orders.signals import order_canceled

        user, order = self.make_earning_order()
        services.debit_points(user, 50, LoyaltyTransaction.ADMIN_DEBIT, 'کسر دستی نمونه')

        with self.assertLogs('loyalty.receivers', level='ERROR') as captured:
            order_canceled.send_robust(sender=OrderModel, order=order)   # نباید استثنا به بیرون بدهد

        self.assertTrue(any(f'سفارش #{order.id}' in message for message in captured.output))
        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 50)   # دست‌نخورده - چرخه‌ی لغو سفارش خودش هم اصلاً دست نخورده


# ============================================================================== مسیر واقعی لغو (اتصال واقعی سیگنال)
class RealCancellationFlowTests(LoyaltyCancellationTestBase):
    def test_real_order_save_transition_to_canceled_triggers_reverse(self):
        user, order = self.make_earning_order()

        with self.captureOnCommitCallbacks(execute=True):
            order.status = 'canceled'
            order.save()

        self.assertEqual(LoyaltyAccount.objects.get(user=user).current_balance, 0)
        self.assertTrue(
            LoyaltyTransaction.objects.filter(
                source_type='order', source_id=order.id, transaction_type=LoyaltyTransaction.REVERSE,
            ).exists(),
        )

    def test_saving_an_already_canceled_order_again_does_not_double_reverse(self):
        """ Order.save() خودش با previous!=canceled دوباره سیگنال نمی‌فرستد - سطح دومِ محافظت. """
        user, order = self.make_earning_order()
        with self.captureOnCommitCallbacks(execute=True):
            order.status = 'canceled'
            order.save()

        with self.captureOnCommitCallbacks(execute=True):
            order.save()   # ذخیره‌ی دوباره‌ی همان سفارشِ از قبل canceled (بدون تغییر فیلد دیگری)

        self.assertEqual(
            LoyaltyTransaction.objects.filter(
                source_type='order', source_id=order.id, transaction_type=LoyaltyTransaction.REVERSE,
            ).count(),
            1,
        )


# ============================================================================== هم‌زمانی واقعی
class CancellationConcurrencyTests(TransactionTestCase):
    """ دو نخ/دو اتصال دیتابیس جدا هم‌زمان تلاش می‌کنند همان سفارش را برگردانند. """

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

    def test_two_concurrent_cancellation_reverses_never_double_reverse(self):
        cache.delete(SiteSettings.CACHE_KEY)
        settings_obj = SiteSettings.load()
        settings_obj.loyalty_mode = SiteSettings.LOYALTY_MODE_ORDER_COUNT
        settings_obj.loyalty_points_per_order = 100
        settings_obj.loyalty_activated_at = timezone.now() - timedelta(days=1)
        settings_obj.save()

        user = CustomUser.objects.create_user(phone_number='09120819999')
        order = Order.objects.create(
            user=user, first_name='کاربر', last_name='تست', phone=user.phone_number,
            address='تهران', payment_method='cash', total_price=100000, status='pending',
        )
        OrderItem.objects.create(order=order, price=100000, quantity=1)
        txn = Transaction.objects.create(
            user=user, order=order, amount=order.total_price, authority='TEST-CANCEL-RACE', status='success',
        )
        earn_txn = earning.earn_from_payment(order, txn)
        self.assertIsNotNone(earn_txn)

        jobs = [(lambda: cancellation.reverse_from_cancellation(order)) for _ in range(6)]
        results = self.run_threads(jobs)

        unexpected = [r for r in results if isinstance(r, BaseException)]
        self.assertEqual(unexpected, [], f'نتایج غیرمنتظره: {unexpected}')

        reverse_results = [r for r in results if isinstance(r, LoyaltyTransaction)]
        pks = {r.pk for r in reverse_results}
        self.assertEqual(len(pks), 1)   # فقط یک رکورد REVERSE واقعی ساخته شده

        self.assertEqual(LoyaltyAccount.objects.get(user=user).current_balance, 0)   # نه منفی
        self.assertEqual(
            LoyaltyTransaction.objects.filter(
                source_type='order', source_id=order.id, transaction_type=LoyaltyTransaction.REVERSE,
            ).count(),
            1,
        )

        settings_obj.loyalty_activated_at = None
        settings_obj.save()


class CombinedCancellationAndReturnTests(LoyaltyCancellationTestBase):
    """
    رگرسیون مستقیم روی یافته‌ی ممیزی پیش‌کامیت فاز ۲ (bullet ۳): _already_reversed_amount در
    cancellation.py حالا باید بازگشت‌های ناشی از مرجوعی را هم ببیند - وگرنه ترکیب «مرجوعی سپس
    لغو» می‌توانست جمع بازگشت‌ها را از E عبور دهد.
    """

    def _make_earning_order(self, *, quantity, unit_price, points_per_order=1000):
        settings_obj = SiteSettings.load()
        settings_obj.loyalty_mode = SiteSettings.LOYALTY_MODE_ORDER_COUNT
        settings_obj.loyalty_points_per_order = points_per_order
        settings_obj.save()

        user = _make_user()
        order = _make_order(user, item_price=unit_price, item_qty=quantity)
        self.activate_before(order.created_at)
        txn = _pay(order)
        earn_txn = earning.earn_from_payment(order, txn)
        self.assertEqual(earn_txn.amount, points_per_order)
        return user, order

    def _make_return_reverse(self, order, user, order_item, approved_quantity, refund_amount):
        """ می‌سازد و بلافاصله (مثل رسیدن سیگنال return_refund_completed) بازگشتش را اعمال می‌کند. """
        reason = ReturnReason.objects.create(title='دلیل تست ترکیبی')
        rr = ReturnRequest.objects.create(order=order, user=user, refund_method=ReturnRequest.REFUND_WALLET)
        ReturnItem.objects.create(
            return_request=rr, order_item=order_item, reason=reason,
            requested_quantity=approved_quantity, approved_quantity=approved_quantity,
            refund_amount=Decimal(refund_amount),
        )
        reverse_from_return(rr)
        return rr

    def test_return_then_cancel_only_reverses_the_remainder(self):
        """ سناریوی ترکیبی ۱: E=1000، مرجوعی جزئی ۳۰٪ (۳۰۰ امتیاز)، سپس لغو - باید دقیقاً ۷۰۰ کسر کند، نه ۱۰۰۰. """
        user, order = self._make_earning_order(quantity=10, unit_price=100000)   # B=1,000,000
        order_item = order.items.get()

        self._make_return_reverse(order, user, order_item, approved_quantity=3, refund_amount='300000')   # ۳۰٪ → ۳۰۰
        self.assertEqual(LoyaltyAccount.objects.get(user=user).current_balance, 700)

        result = cancellation.reverse_from_cancellation(order)

        self.assertIsNotNone(result)
        self.assertEqual(result.amount, -700)   # نه -۱۰۰۰ - دقیقاً باقی‌مانده‌ی واقعی
        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 0)
        self.assertEqual(account.lifetime_earned, 1000)   # دست‌نخورده

        total_reversed = -sum(
            LoyaltyTransaction.objects.filter(transaction_type=LoyaltyTransaction.REVERSE).values_list('amount', flat=True)
        )
        self.assertEqual(total_reversed, 1000)   # دقیقاً برابر E (۳۰۰ مرجوعی + ۷۰۰ لغو)، نه بیشتر

    def test_cancel_then_return_signal_is_a_noop_after_full_cancellation_reverse(self):
        """ سناریوی ترکیبی ۲: لغو کل E را برمی‌گرداند؛ سیگنال مرجوعیِ بعدی دیگر چیزی کسر نمی‌کند (to_reverse=0). """
        user, order = self._make_earning_order(quantity=10, unit_price=100000)
        order_item = order.items.get()

        cancel_result = cancellation.reverse_from_cancellation(order)
        self.assertEqual(cancel_result.amount, -1000)
        self.assertEqual(LoyaltyAccount.objects.get(user=user).current_balance, 0)

        # حالا (سناریوی لبه‌ای) یک سیگنال مرجوعی برای همین سفارش می‌رسد
        reason = ReturnReason.objects.create(title='دلیل تست ترکیبی ۲')
        rr = ReturnRequest.objects.create(order=order, user=user, refund_method=ReturnRequest.REFUND_WALLET)
        ReturnItem.objects.create(
            return_request=rr, order_item=order_item, reason=reason,
            requested_quantity=3, approved_quantity=3, refund_amount=Decimal('300000'),
        )

        result = reverse_from_return(rr)

        self.assertIsNone(result)   # to_reverse=0 - remaining دیگر صفر است
        self.assertEqual(LoyaltyAccount.objects.get(user=user).current_balance, 0)   # همچنان صفر، نه منفی
        total_reversed = -sum(
            LoyaltyTransaction.objects.filter(transaction_type=LoyaltyTransaction.REVERSE).values_list('amount', flat=True)
        )
        self.assertEqual(total_reversed, 1000)   # هرگز از E عبور نکرد
