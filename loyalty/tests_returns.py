"""
تست‌های Loyalty Phase 2D: بازگشت نسبی امتیاز از مرجوعی کالا
(loyalty/returns.py + loyalty/receivers.py::on_return_refund_completed).

هم‌الگوی loyalty/tests_earning.py و loyalty/tests_cancellation.py برای مدیریت کش SiteSettings.
از returns.tests.ReturnsTestMixin (هلپرهای موجود خودِ اپ returns) استفاده می‌کند - فقط خوانده
می‌شود، هیچ فایلی در returns تغییر نمی‌کند.
"""

import threading
from decimal import Decimal

from django.core.cache import cache
from django.db import connection
from django.db.models import Sum
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from accounts.models import CustomUser
from orders.models import OrderItem
from payments.models import Transaction
from products.models import SiteSettings
from returns import services as return_services
from returns.models import ReturnItem, ReturnRequest
from returns.refund_calculator import is_full_order_return
from returns.tests import ReturnsTestMixin

from . import earning
from .models import LoyaltyAccount, LoyaltyTransaction
from .returns import calculate_reverse_amount, reverse_from_return


class LoyaltyReturnsTestBase(ReturnsTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        cache.delete(SiteSettings.CACHE_KEY)
        settings_obj = SiteSettings.load()
        settings_obj.loyalty_mode = SiteSettings.LOYALTY_MODE_ORDER_COUNT
        settings_obj.loyalty_points_per_order = 1000
        settings_obj.loyalty_activated_at = timezone.now() - timezone.timedelta(days=1)
        settings_obj.save()
        self.addCleanup(self._reset_settings)

    def _reset_settings(self):
        obj = SiteSettings.load()
        obj.loyalty_mode = SiteSettings.LOYALTY_MODE_ORDER_COUNT
        obj.loyalty_points_per_order = 100
        obj.loyalty_activated_at = None
        obj.save()
        cache.delete(SiteSettings.CACHE_KEY)

    def make_earning_order(self, *, quantity=10, unit_price=100000, admin_phone='09140009001', shipping_cost=0):
        """
        سفارش تحویل‌شده با یک OrderItem (پس order.items_total = quantity × unit_price = B)،
        پرداخت موفق و یک EARN_ORDER واقعی (E=1000، از loyalty_points_per_order بالا).
        """
        user = self.make_user()
        category = self.make_category()
        product = self.make_product(category)
        order = self.make_order(user, shipping_cost=shipping_cost)
        order_item = self.make_order_item(order, product, quantity=quantity, price=unit_price)
        reason = self.make_reason()
        admin_user = CustomUser.objects.create_superuser(phone_number=admin_phone)

        txn = Transaction.objects.create(
            user=user, order=order, amount=order.total_price, authority=f'TEST-RETURN-{order.id}', status='success',
        )
        earn_txn = earning.earn_from_payment(order, txn)
        self.assertEqual(earn_txn.amount, 1000)

        return order, user, order_item, reason, admin_user

    def run_full_return_via_services(self, order, user, order_item, reason, quantity, admin_user, *, refund_method='wallet'):
        """ چرخه‌ی کامل واقعی از returns/services.py: create -> approve -> received -> refund_pending -> complete. """
        with self.captureOnCommitCallbacks(execute=True):
            rr = return_services.create_return_request(
                order, user, [{'order_item': order_item, 'reason': reason, 'requested_quantity': quantity}],
                refund_method=refund_method,
            )
        with self.captureOnCommitCallbacks(execute=True):
            return_services.approve_return_request(rr, admin_user)
        item = rr.items.get()
        with self.captureOnCommitCallbacks(execute=True):
            return_services.mark_items_received(rr, {item.pk: quantity}, admin_user)
        with self.captureOnCommitCallbacks(execute=True):
            return_services.mark_refund_pending(rr)
        with self.captureOnCommitCallbacks(execute=True):
            return_services.complete_refund(rr, admin_user)
        rr.refresh_from_db()
        return rr


# ============================================================================== فرمول تسهیم نسبی
class SingleReturnRatioTests(LoyaltyReturnsTestBase):
    def test_single_partial_return_reverses_exact_ratio(self):
        order, user, order_item, reason, admin_user = self.make_earning_order(quantity=10, unit_price=100000)

        rr = self.run_full_return_via_services(order, user, order_item, reason, 3, admin_user)   # ۳ از ۱۰ واحد = ۳۰٪

        self.assertEqual(rr.status, ReturnRequest.STATUS_COMPLETED)
        self.assertFalse(is_full_order_return(order))   # فقط ۳۰٪ - نه کامل
        self.assertEqual(
            LoyaltyTransaction.objects.filter(
                source_type='return_request', source_id=rr.id, transaction_type=LoyaltyTransaction.REVERSE,
            ).count(),
            1,
        )
        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 700)   # ۱۰۰۰ - ۳۰۰ (۳۰٪ دقیق)
        self.assertEqual(account.lifetime_earned, 1000)
        self.assertEqual(account.lifetime_redeemed, 300)


class SequentialPartialReturnsTests(LoyaltyReturnsTestBase):
    def test_thirty_then_twenty_then_fifty_percent_never_exceeds_original_points(self):
        """ سناریوی دقیق دستورالعمل: ۳۰٪ سپس ۲۰٪ سپس ۵۰٪ (که کل سفارش را کامل می‌کند). """
        order, user, order_item, reason, admin_user = self.make_earning_order(quantity=10, unit_price=100000)

        first = self.run_full_return_via_services(order, user, order_item, reason, 3, admin_user)   # ۳۰٪ → ۳۰۰
        self.assertEqual(LoyaltyAccount.objects.get(user=user).current_balance, 700)

        # همان قلم (ظرفیت ۱۰ واحد)، سه درخواست جدا و متوالی: ۳+۲+۵=۱۰ - returns/refund_calculator.py:
        # get_returnable_quantity ظرفیت باقیمانده را بعد از هر COMPLETED به‌روز می‌بیند (۷ سپس ۵).
        second = self.run_full_return_via_services(order, user, order_item, reason, 2, admin_user)   # ۲۰٪ بیشتر → ۲۰۰
        self.assertEqual(LoyaltyAccount.objects.get(user=user).current_balance, 500)

        third = self.run_full_return_via_services(order, user, order_item, reason, 5, admin_user)   # ۵ واحد باقی‌مانده = کامل
        self.assertTrue(is_full_order_return(order))
        self.assertEqual(LoyaltyAccount.objects.get(user=user).current_balance, 0)   # دقیقاً صفر، نه منفی

        total_reversed = -sum(
            LoyaltyTransaction.objects.filter(
                source_type='return_request', source_id__in=[first.id, second.id, third.id],
                transaction_type=LoyaltyTransaction.REVERSE,
            ).values_list('amount', flat=True)
        )
        self.assertEqual(total_reversed, 1000)   # دقیقاً برابر E، نه بیشتر
        self.assertEqual(LoyaltyAccount.objects.get(user=user).lifetime_earned, 1000)   # دست‌نخورده


class ShippingExclusionTests(LoyaltyReturnsTestBase):
    def test_shipping_refund_amount_never_enters_the_ratio(self):
        """
        تست ایزوله‌ی فرمول calculate_reverse_amount: حتی اگر (به‌صورت مصنوعی، نه از مسیر واقعی
        returns/services.py) shipping_refund_amount مقدار بزرگی داشته باشد، وقتی مرجوعی *کامل*
        نیست، این مقدار مطلقاً وارد R نمی‌شود.
        """
        order, user, order_item, reason, admin_user = self.make_earning_order(quantity=10, unit_price=100000)

        rr = ReturnRequest.objects.create(order=order, user=user, refund_method=ReturnRequest.REFUND_WALLET)
        ReturnItem.objects.create(
            return_request=rr, order_item=order_item, reason=reason,
            requested_quantity=3, approved_quantity=3, refund_amount=Decimal('300000'),   # ۳۰٪ از B=1,000,000
        )
        rr.shipping_refund_amount = Decimal('999999')   # عمداً بزرگ و غیرواقعی؛ نباید هیچ اثری داشته باشد
        rr.save(update_fields=['shipping_refund_amount'])

        self.assertFalse(is_full_order_return(order))   # فقط ۳ از ۱۰ واحد - قطعاً کامل نیست
        self.assertEqual(calculate_reverse_amount(rr), 300)   # نه چیزی نزدیک به ۹۹۹۲۹۹ یا بیشتر از ۱۰۰۰

    def test_full_return_shipping_refund_takes_the_exact_remainder_not_the_ratio(self):
        """ برای تکمیل: در مرجوعیِ *کامل*، فرمول نسبی اصلاً اجرا نمی‌شود - فقط remaining دقیق. """
        order, user, order_item, reason, admin_user = self.make_earning_order(
            quantity=10, unit_price=100000, shipping_cost=50000,   # کرایه‌ی غیرصفر تا استرداد کرایه هم واقعاً رخ دهد
        )
        rr = self.run_full_return_via_services(order, user, order_item, reason, 10, admin_user)   # کل سفارش یک‌جا

        self.assertTrue(is_full_order_return(order))
        self.assertGreater(rr.shipping_refund_amount, 0)   # این مرجوعی واقعاً هزینه‌ی ارسال را هم برگردانده
        self.assertEqual(LoyaltyAccount.objects.get(user=user).current_balance, 0)   # کل ۱۰۰۰ برگشته، نه کم‌تر/بیشتر


# ============================================================================== حالت‌های بدون Earn
class NoEarnTests(LoyaltyReturnsTestBase):
    def test_return_on_order_without_any_earn_is_a_silent_noop(self):
        # مرز فعال‌سازی را *بعد* از ثبت سفارش می‌گذاریم تا earn_from_payment هیچ EARN_ORDER ای نسازد
        settings_obj = SiteSettings.load()
        settings_obj.loyalty_activated_at = timezone.now() + timezone.timedelta(days=1)
        settings_obj.save()

        user = self.make_user()
        category = self.make_category()
        product = self.make_product(category)
        order = self.make_order(user)
        order_item = self.make_order_item(order, product, quantity=10, price=100000)
        reason = self.make_reason()
        admin_user = CustomUser.objects.create_superuser(phone_number='09140009002')
        Transaction.objects.create(
            user=user, order=order, amount=order.total_price, authority='TEST-NO-EARN', status='success',
        )
        self.assertFalse(LoyaltyTransaction.objects.filter(source_type='order', source_id=order.id).exists())

        rr = self.run_full_return_via_services(order, user, order_item, reason, 3, admin_user)

        self.assertIsNone(reverse_from_return(rr))
        self.assertFalse(LoyaltyTransaction.objects.filter(transaction_type=LoyaltyTransaction.REVERSE).exists())
        self.assertFalse(LoyaltyAccount.objects.filter(user=user).exists())


# ============================================================================== ایدمپوتنسی
class DuplicateSignalTests(LoyaltyReturnsTestBase):
    def test_duplicate_call_for_the_same_return_request_reverses_only_once(self):
        """
        بر خلاف بازگشت لغو (loyalty/cancellation.py که با remaining<=0 حتی پیش از رسیدن به
        debit_points متوقف می‌شود)، اینجا فرمول نسبی برای همان return_request دوباره همان مقدار
        را حساب می‌کند - محافظت از idempotency_key در خودِ debit_points می‌آید: هر دو فراخوانی
        دقیقاً به یک رکورد واحد اشاره می‌کنند، نه اینکه فراخوانی دوم None برگرداند.
        """
        order, user, order_item, reason, admin_user = self.make_earning_order(quantity=10, unit_price=100000)
        rr = self.run_full_return_via_services(order, user, order_item, reason, 3, admin_user)
        first = LoyaltyTransaction.objects.get(source_type='return_request', source_id=rr.id, transaction_type=LoyaltyTransaction.REVERSE)

        second = reverse_from_return(rr)   # مثل شلیک مجدد سیگنال برای همان درخواست مرجوعی

        self.assertIsNotNone(second)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(
            LoyaltyTransaction.objects.filter(
                source_type='return_request', source_id=rr.id, transaction_type=LoyaltyTransaction.REVERSE,
            ).count(),
            1,
        )
        self.assertEqual(LoyaltyAccount.objects.get(user=user).current_balance, 700)   # فقط یک‌بار کسر شده، نه دوبار

    def test_signal_fired_directly_twice_reverses_only_once(self):
        from returns.signals import return_refund_completed

        order, user, order_item, reason, admin_user = self.make_earning_order(quantity=10, unit_price=100000)
        rr = self.run_full_return_via_services(order, user, order_item, reason, 3, admin_user)

        return_refund_completed.send_robust(sender=ReturnRequest, return_request=rr)
        return_refund_completed.send_robust(sender=ReturnRequest, return_request=rr)

        self.assertEqual(
            LoyaltyTransaction.objects.filter(
                source_type='return_request', source_id=rr.id, transaction_type=LoyaltyTransaction.REVERSE,
            ).count(),
            1,
        )
        self.assertEqual(LoyaltyAccount.objects.get(user=user).current_balance, 700)


# ============================================================================== کسری موجودی و ایزوله‌سازی خطا
class InsufficientBalanceTests(LoyaltyReturnsTestBase):
    def test_receiver_catches_and_logs_insufficient_balance_without_crashing_the_refund_cycle(self):
        from returns.signals import return_refund_completed

        order, user, order_item, reason, admin_user = self.make_earning_order(quantity=10, unit_price=100000)
        rr = self.run_full_return_via_services(order, user, order_item, reason, 3, admin_user)   # ۷۰۰ باقی می‌ماند

        from . import services
        services.debit_points(user, 700, LoyaltyTransaction.ADMIN_DEBIT, 'کسر دستی نمونه برای آزمون')   # موجودی صفر

        # یک درخواست مرجوعی دیگر برای همان سفارش که نظری باید امتیاز برگرداند ولی موجودی ندارد
        rr2 = self.run_full_return_via_services(order, user, order_item, reason, 2, admin_user, refund_method='wallet')

        with self.assertLogs('loyalty.receivers', level='ERROR') as captured:
            return_refund_completed.send_robust(sender=ReturnRequest, return_request=rr2)

        self.assertTrue(any(f'مرجوعی #{rr2.id}' in message for message in captured.output))
        self.assertEqual(LoyaltyAccount.objects.get(user=user).current_balance, 0)   # دست‌نخورده، منفی نشده


# ============================================================================== هم‌زمانی واقعی
class ReturnReverseConcurrencyTests(TransactionTestCase):
    """
    دو نخ/دو اتصال دیتابیس جدا هم‌زمان برای *دو درخواست مرجوعی متفاوت* از همان سفارش تلاش
    می‌کنند بازگشت بگیرند - جمع مقادیری که هرکدام مستقل محاسبه می‌کنند از E بیشتر است، تا اثبات
    شود سقف واقعی (موجودی/قفل ردیفی) هرگز اجازه‌ی عبور از E را نمی‌دهد.
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

    def test_two_concurrent_returns_never_reverse_more_than_the_original_earn(self):
        cache.delete(SiteSettings.CACHE_KEY)
        settings_obj = SiteSettings.load()
        settings_obj.loyalty_mode = SiteSettings.LOYALTY_MODE_ORDER_COUNT
        settings_obj.loyalty_points_per_order = 1000
        settings_obj.loyalty_activated_at = timezone.now() - timezone.timedelta(days=1)
        settings_obj.save()

        mixin = ReturnsTestMixin()
        user = mixin.make_user('09140099001')
        category = mixin.make_category(name='دسته هم‌زمانی', slug='loyalty-return-race-cat')
        product = mixin.make_product(category, name='کالای هم‌زمانی', slug='loyalty-return-race-p', erp_code='ERP-RETRACE-1')
        order = mixin.make_order(user)
        order_item = OrderItem.objects.create(order=order, product=product, price=100000, quantity=10)   # B=1,000,000

        txn = Transaction.objects.create(
            user=user, order=order, amount=order.total_price, authority='TEST-RETURN-RACE', status='success',
        )
        earn_txn = earning.earn_from_payment(order, txn)
        self.assertEqual(earn_txn.amount, 1000)

        # دو ReturnRequest مستقل، هرکدام مستقیماً (بدون عبور از گارد ظرفیت واقعی returns/services.py -
        # این تست فقط ایمنی سقف E در loyalty را می‌سنجد، نه قواعد ظرفیت returns) با ۷۰۰۰۰۰
        # ریفاند اقلام (۷۰٪ از B) - اگر هر دو کامل اعمال شوند جمعاً ۱۴۰٪ می‌شود، که نباید رخ دهد.
        reason = mixin.make_reason()
        return_requests = []
        for _ in range(2):
            rr = ReturnRequest.objects.create(order=order, user=user, refund_method=ReturnRequest.REFUND_WALLET)
            ReturnItem.objects.create(
                return_request=rr, order_item=order_item, reason=reason,
                requested_quantity=7, approved_quantity=7, refund_amount=Decimal('700000'),
            )
            return_requests.append(rr)

        jobs = [(lambda rr=rr: reverse_from_return(rr)) for rr in return_requests]
        results = self.run_threads(jobs)

        from .exceptions import InsufficientPointsError
        unexpected = [r for r in results if not isinstance(r, (LoyaltyTransaction, InsufficientPointsError, type(None)))]
        self.assertEqual(unexpected, [], f'نتایج غیرمنتظره: {unexpected}')

        total_reversed = -(
            LoyaltyTransaction.objects.filter(
                source_type='return_request', source_id__in=[rr.id for rr in return_requests],
                transaction_type=LoyaltyTransaction.REVERSE,
            ).aggregate(total=Sum('amount'))['total'] or 0
        )
        self.assertLessEqual(total_reversed, 1000)   # هرگز از E بیشتر نمی‌شود

        account = LoyaltyAccount.objects.get(user=user)
        self.assertGreaterEqual(account.current_balance, 0)   # هرگز منفی

        settings_obj.loyalty_activated_at = None
        settings_obj.save()
