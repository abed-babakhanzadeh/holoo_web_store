"""
تست‌های Loyalty Phase 4B: موتور تبدیل امتیاز به کوپن/پاداش (loyalty/reward_redemption.py).

عمداً هیچ‌کدام از این تست‌ها هسته‌ی لجر وفاداری (loyalty/services.py) یا هسته‌ی promotions
(promotions/models.py) را مستقیم تغییر نمی‌دهند - فقط ارکستریتور جدید و LoyaltyReward را می‌سنجند.
"""

import itertools
import threading
from unittest import mock

from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from accounts.models import CustomUser
from accounts.testing import make_approved_user
from cart.models import Cart, CartItem
from cart.pricing import price_cart
from loyalty import services
from loyalty.exceptions import (
    IdempotencyKeyConflictError, InsufficientPointsError, RewardAlreadyRedeemedError,
    RewardInactiveError, RewardOutOfStockError,
)
from loyalty.models import LoyaltyAccount, LoyaltyReward, LoyaltyTransaction
from loyalty.reward_redemption import redeem_points_for_reward
from orders.shipping import ShippingQuote
from products.models import Category, Product
from products.pricing import CHECK
from promotions.coupons import evaluate_coupon
from promotions.models import Coupon, CouponRedemption, UserCoupon
from promotions.testing import make_coupon

_seq = itertools.count(1)


def _make_user():
    return CustomUser.objects.create_user(phone_number=f'0912072{next(_seq):04d}')


def _make_reward(points_cost=100, coupon=None, **coupon_fields):
    coupon = coupon or make_coupon(f'REWARD-{next(_seq)}', audience=Coupon.AUDIENCE_ASSIGNED, **coupon_fields)
    return LoyaltyReward.objects.create(title=f'پاداش تست {next(_seq)}', coupon=coupon, points_cost=points_cost)


class RewardRedemptionTestBase(TestCase):
    def _give_points(self, user, amount):
        return services.credit_points(user, amount, LoyaltyTransaction.EARN_ORDER, 'کسب تست')


class SuccessfulRewardRedemptionTests(RewardRedemptionTestBase):
    def test_successful_redemption_debits_loyalty_and_assigns_the_coupon(self):
        user = _make_user()
        self._give_points(user, 300)
        reward = _make_reward(points_cost=100)

        loyalty_txn, user_coupon = redeem_points_for_reward(user, reward, idempotency_key='reward-1')

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 200)
        self.assertEqual(loyalty_txn.amount, -100)
        self.assertEqual(loyalty_txn.transaction_type, LoyaltyTransaction.REDEEM_REWARD)
        self.assertEqual(loyalty_txn.source_type, 'loyalty_reward')
        self.assertEqual(loyalty_txn.source_id, reward.pk)

        self.assertEqual(user_coupon.coupon_id, reward.coupon_id)
        self.assertEqual(user_coupon.user_id, user.id)
        self.assertEqual(user_coupon.source, UserCoupon.SOURCE_AUTO)

    def test_lifetime_earned_untouched_lifetime_redeemed_increases(self):
        user = _make_user()
        self._give_points(user, 300)
        reward = _make_reward(points_cost=100)

        redeem_points_for_reward(user, reward, idempotency_key='reward-2')

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.lifetime_earned, 300)   # دست‌نخورده
        self.assertEqual(account.lifetime_redeemed, 100)


class ValidationErrorTests(RewardRedemptionTestBase):
    def test_insufficient_points_raises_and_rolls_back(self):
        user = _make_user()
        self._give_points(user, 50)
        reward = _make_reward(points_cost=100)

        with self.assertRaises(InsufficientPointsError):
            redeem_points_for_reward(user, reward, idempotency_key='reward-insufficient')

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 50)
        self.assertFalse(UserCoupon.objects.filter(user=user).exists())

    def test_inactive_reward_is_rejected_without_touching_the_ledger(self):
        user = _make_user()
        self._give_points(user, 300)
        reward = _make_reward(points_cost=100, is_active=False)

        with self.assertRaises(RewardInactiveError):
            redeem_points_for_reward(user, reward, idempotency_key='reward-inactive')

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 300)

    def test_inactive_coupon_behind_the_reward_is_rejected(self):
        user = _make_user()
        self._give_points(user, 300)
        reward = _make_reward(points_cost=100, active=False)

        with self.assertRaises(RewardInactiveError):
            redeem_points_for_reward(user, reward, idempotency_key='reward-coupon-inactive')

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 300)

    def test_expired_coupon_behind_the_reward_is_rejected(self):
        user = _make_user()
        self._give_points(user, 300)
        reward = _make_reward(points_cost=100, expired=True)

        with self.assertRaises(RewardInactiveError):
            redeem_points_for_reward(user, reward, idempotency_key='reward-coupon-expired')

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 300)


class CapacityTests(RewardRedemptionTestBase):
    def test_claim_limit_exhausted_rolls_back_the_debit(self):
        user = _make_user()
        self._give_points(user, 300)
        reward = _make_reward(points_cost=100, claim_limit=1)
        other_user = _make_user()
        UserCoupon.objects.create(coupon=reward.coupon, user=other_user, source=UserCoupon.SOURCE_AUTO)   # ظرفیت را پر می‌کند

        with self.assertRaises(RewardOutOfStockError):
            redeem_points_for_reward(user, reward, idempotency_key='reward-claim-full')

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 300)   # کسر رول‌بک شد
        self.assertFalse(UserCoupon.objects.filter(user=user).exists())

    def test_total_limit_exhausted_by_redeemed_usage_rolls_back_the_debit(self):
        user = _make_user()
        self._give_points(user, 300)
        reward = _make_reward(points_cost=100, total_limit=1)
        CouponRedemption.objects.create(
            coupon=reward.coupon, order_id=90001, code=reward.coupon.code, status=CouponRedemption.STATUS_REDEEMED,
        )

        with self.assertRaises(RewardOutOfStockError):
            redeem_points_for_reward(user, reward, idempotency_key='reward-total-full')

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 300)

    def test_capacity_check_ignores_expired_reservations(self):
        user = _make_user()
        self._give_points(user, 300)
        reward = _make_reward(points_cost=100, total_limit=1)
        CouponRedemption.objects.create(
            coupon=reward.coupon, order_id=90002, code=reward.coupon.code, status=CouponRedemption.STATUS_RESERVED,
            expires_at=timezone.now() - timezone.timedelta(minutes=5),   # رزرو منقضی‌شده - نباید ظرفیت را اشغال کند
        )

        loyalty_txn, user_coupon = redeem_points_for_reward(user, reward, idempotency_key='reward-expired-reservation')
        self.assertEqual(loyalty_txn.amount, -100)


class RollbackOnUnexpectedFailureTests(RewardRedemptionTestBase):
    def test_no_raw_integrity_error_ever_reaches_the_caller(self):
        """ اگر UserCoupon.objects.create به هر دلیلی شکست بخورد، خطای دامنه‌ای دریافت شود نه IntegrityError خام. """
        user = _make_user()
        self._give_points(user, 300)
        reward = _make_reward(points_cost=100)

        with mock.patch('loyalty.reward_redemption.UserCoupon.objects.create', side_effect=RuntimeError('خطای فرضی')):
            with self.assertRaises(RuntimeError):
                redeem_points_for_reward(user, reward, idempotency_key='reward-assign-fail')

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 300)   # کسر امتیاز هم رول‌بک شد
        self.assertFalse(LoyaltyTransaction.objects.filter(idempotency_key='reward-assign-fail').exists())


class IdempotencyTests(RewardRedemptionTestBase):
    def test_sequential_replay_returns_the_same_pair_without_double_effect(self):
        user = _make_user()
        self._give_points(user, 300)
        reward = _make_reward(points_cost=100)

        first_txn, first_coupon = redeem_points_for_reward(user, reward, idempotency_key='reward-replay')
        second_txn, second_coupon = redeem_points_for_reward(user, reward, idempotency_key='reward-replay')

        self.assertEqual(first_txn.pk, second_txn.pk)
        self.assertEqual(first_coupon.pk, second_coupon.pk)
        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 200)   # فقط یک‌بار کسر شد
        self.assertEqual(UserCoupon.objects.filter(coupon=reward.coupon, user=user).count(), 1)

    def test_idempotency_conflict_for_a_different_reward_with_the_same_cost(self):
        """ رگرسیون مستقیم روی باگ Double-Reward که در بازبینی طراحی کشف شد. """
        user = _make_user()
        self._give_points(user, 300)
        reward_a = _make_reward(points_cost=100)
        reward_b = _make_reward(points_cost=100)   # هم‌قیمت با reward_a

        redeem_points_for_reward(user, reward_a, idempotency_key='reward-conflict')

        with self.assertRaises(IdempotencyKeyConflictError):
            redeem_points_for_reward(user, reward_b, idempotency_key='reward-conflict')

        # پاداش دوم هرگز صادر نشد و امتیازِ اضافه‌ای هم کسر نشد
        self.assertFalse(UserCoupon.objects.filter(coupon=reward_b.coupon, user=user).exists())
        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 200)   # فقط کسر reward_a

    def test_idempotency_conflict_error_for_a_different_user(self):
        user_a = _make_user()
        user_b = _make_user()
        self._give_points(user_a, 300)
        self._give_points(user_b, 300)
        reward = _make_reward(points_cost=100)

        redeem_points_for_reward(user_a, reward, idempotency_key='reward-user-conflict')
        with self.assertRaises(IdempotencyKeyConflictError):
            redeem_points_for_reward(user_b, reward, idempotency_key='reward-user-conflict')

        self.assertFalse(UserCoupon.objects.filter(coupon=reward.coupon, user=user_b).exists())


class ConcurrencyTests(TransactionTestCase):
    """ هم‌الگوی loyalty/tests_redemption.py::ConcurrencyTests - نخ‌های واقعی، هرکدام اتصال دیتابیس مستقل خودش. """

    def run_threads(self, jobs):
        barrier = threading.Barrier(len(jobs))
        results = [None] * len(jobs)

        def worker(index_, job):
            try:
                barrier.wait(timeout=30)
                results[index_] = job()
            except BaseException as error:   # noqa: BLE001
                results[index_] = error
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=(i, job)) for i, job in enumerate(jobs)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertFalse(any(t.is_alive() for t in threads), 'یک نخ گیر کرد (احتمال deadlock)')
        return results

    def test_concurrent_redemption_same_user_same_reward_different_keys(self):
        """ سناریوی حیاتیِ رفع Double-Debit Race: کلیدهای *متفاوت*، همان کاربر، همان پاداش. """
        user = CustomUser.objects.create_user(phone_number='09120721111')
        services.credit_points(user, 300, LoyaltyTransaction.EARN_ORDER, 'موجودی اولیه')
        reward = _make_reward(points_cost=100)

        jobs = [
            (lambda i=i: redeem_points_for_reward(user, reward, idempotency_key=f'concurrent-reward-key-{i}'))
            for i in range(10)
        ]
        results = self.run_threads(jobs)

        succeeded = [r for r in results if isinstance(r, tuple)]
        failed = [r for r in results if isinstance(r, RewardAlreadyRedeemedError)]
        unexpected = [r for r in results if not isinstance(r, (tuple, RewardAlreadyRedeemedError))]

        self.assertEqual(unexpected, [], f'نتایج غیرمنتظره: {unexpected}')
        self.assertEqual(len(succeeded), 1, 'فقط یک نخ باید موفق شود')
        self.assertEqual(len(failed), 9)

        self.assertEqual(LoyaltyTransaction.objects.filter(source_type='loyalty_reward', source_id=reward.pk).count(), 1)
        self.assertEqual(UserCoupon.objects.filter(coupon=reward.coupon, user=user).count(), 1)

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 200)   # فقط یک‌بار کسر شد، نه ۱۰ بار

    def test_concurrent_redemption_different_users_single_capacity_reward(self):
        """ چند کاربر مختلف برای یک پاداش با claim_limit=1 - دقیقاً یک برنده. """
        reward = _make_reward(points_cost=100, claim_limit=1)
        users = [CustomUser.objects.create_user(phone_number=f'091207220{i:02d}') for i in range(8)]
        for user in users:
            services.credit_points(user, 300, LoyaltyTransaction.EARN_ORDER, 'موجودی اولیه')

        jobs = [
            (lambda u=u: redeem_points_for_reward(u, reward, idempotency_key=f'concurrent-capacity-{u.pk}'))
            for u in users
        ]
        results = self.run_threads(jobs)

        succeeded = [r for r in results if isinstance(r, tuple)]
        failed = [r for r in results if isinstance(r, RewardOutOfStockError)]
        unexpected = [r for r in results if not isinstance(r, (tuple, RewardOutOfStockError))]

        self.assertEqual(unexpected, [], f'نتایج غیرمنتظره: {unexpected}')
        self.assertEqual(len(succeeded), 1, 'فقط یک کاربر باید برنده شود')
        self.assertEqual(len(failed), 7)
        self.assertEqual(UserCoupon.objects.filter(coupon=reward.coupon).count(), 1)


# ============================================================================================
# Loyalty Phase 4C: راستی‌آزمایی جامع کوپن‌های ارسال رایگان باشگاه (Free-Shipping Voucher
# Integration Tests). هیچ کد پروداکشنی برای این زیرفاز تغییر نکرد - صرفاً اثبات این‌که معماری
# فاز ۴B (بدون هیچ تغییری) برای kind=KIND_FREE_SHIPPING هم درست کار می‌کند، تا زنجیره‌ی کامل
# «بازخرید با امتیاز» ← «ارزیابی در چک‌اوت» (promotions.coupons.evaluate_coupon) را بپوشاند.
# ============================================================================================

def _make_free_shipping_reward(points_cost=100, **coupon_fields):
    coupon_fields.setdefault('claim_limit', None)
    coupon_fields.setdefault('total_limit', None)
    coupon = make_coupon(
        f'SHIP-REWARD-{next(_seq)}', kind=Coupon.KIND_FREE_SHIPPING, scope=Coupon.SCOPE_CART,
        audience=Coupon.AUDIENCE_ASSIGNED, is_claimable=False, **coupon_fields,
    )
    return LoyaltyReward.objects.create(title=f'پاداش ارسال رایگان {next(_seq)}', coupon=coupon, points_cost=points_cost)


class FreeShippingRewardRedemptionTests(RewardRedemptionTestBase):
    """ سناریوی ۱ (مصوبه‌ی ۴C): بازخرید موفق یک پاداش با کوپن kind=free_shipping. """

    def test_successful_free_shipping_redemption(self):
        user = _make_user()
        self._give_points(user, 300)
        reward = _make_free_shipping_reward(points_cost=100)
        self.assertEqual(reward.coupon.kind, Coupon.KIND_FREE_SHIPPING)
        self.assertEqual(reward.coupon.scope, Coupon.SCOPE_CART)
        self.assertEqual(reward.coupon.audience, Coupon.AUDIENCE_ASSIGNED)
        self.assertFalse(reward.coupon.is_claimable)

        loyalty_txn, user_coupon = redeem_points_for_reward(user, reward, idempotency_key='ship-reward-1')

        self.assertEqual(loyalty_txn.transaction_type, LoyaltyTransaction.REDEEM_REWARD)
        self.assertEqual(loyalty_txn.amount, -100)
        self.assertEqual(user_coupon.source, UserCoupon.SOURCE_AUTO)
        self.assertEqual(user_coupon.coupon_id, reward.coupon_id)

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 200)
        self.assertEqual(account.lifetime_redeemed, 100)
        self.assertEqual(account.lifetime_earned, 300)   # دست‌نخورده


class FreeShippingCapacityTests(RewardRedemptionTestBase):
    """ سناریوی ۲ (مصوبه‌ی ۴C): سقف/ظرفیت برای کوپن ارسال رایگان دقیقاً مثل سایر کوپن‌ها. """

    def test_claim_limit_exhausted_rolls_back_the_debit(self):
        user = _make_user()
        self._give_points(user, 300)
        reward = _make_free_shipping_reward(points_cost=100, claim_limit=1)
        other_user = _make_user()
        UserCoupon.objects.create(coupon=reward.coupon, user=other_user, source=UserCoupon.SOURCE_AUTO)

        with self.assertRaises(RewardOutOfStockError):
            redeem_points_for_reward(user, reward, idempotency_key='ship-claim-full')

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 300)
        self.assertFalse(UserCoupon.objects.filter(coupon=reward.coupon, user=user).exists())

    def test_total_limit_exhausted_rolls_back_the_debit(self):
        user = _make_user()
        self._give_points(user, 300)
        reward = _make_free_shipping_reward(points_cost=100, total_limit=1)
        CouponRedemption.objects.create(
            coupon=reward.coupon, order_id=90101, code=reward.coupon.code, status=CouponRedemption.STATUS_REDEEMED,
        )

        with self.assertRaises(RewardOutOfStockError):
            redeem_points_for_reward(user, reward, idempotency_key='ship-total-full')

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 300)


class FreeShippingCheckoutIntegrationTests(RewardRedemptionTestBase):
    """
    سناریوی ۳ (مصوبه‌ی ۴C، اجباری): زنجیره‌ی کامل تا چک‌اوت - بعد از بازخرید موفق، همان کد باید
    در promotions.coupons.evaluate_coupon واقعاً معتبر و «ارسال رایگان» تشخیص داده شود؛ و پیش از
    بازخرید، همان کد باید NOT_ASSIGNED بدهد (audience=assigned بدون UserCoupon).
    """

    def setUp(self):
        super().setUp()
        self.user = make_approved_user(f'0912073{next(_seq):04d}', price_level=1)
        self.category = Category.objects.create(name='دسته چک‌اوت ۴C', slug=f'phase4c-cat-{next(_seq)}')
        self.product = Product.objects.create(
            name='کالای چک‌اوت ۴C', slug=f'phase4c-p-{next(_seq)}', erp_code=f'ERP-4C-{next(_seq)}',
            category=self.category, price=200000, stock=10,
        )
        self.cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(cart=self.cart, product=self.product, quantity=1)

    def _pricing(self):
        items = list(self.cart.items.select_related('product').order_by('pk'))
        return price_cart(items, self.user, CHECK, timezone.now())

    def _courier_quote(self, cost=45000):
        return ShippingQuote(available=True, method='courier', cost=cost, label='ارسال با پیک', reason='', message='', free_cart=False)

    def test_redeemed_free_shipping_coupon_is_accepted_and_waives_the_courier_cost(self):
        self._give_points(self.user, 300)
        reward = _make_free_shipping_reward(points_cost=100)

        _, user_coupon = redeem_points_for_reward(self.user, reward, idempotency_key='ship-checkout-1')

        base_quote = self._courier_quote(cost=45000)
        result = evaluate_coupon(reward.coupon, self.user, self._pricing(), base_quote=base_quote, now=timezone.now())

        self.assertTrue(result.ok, msg=result.message)
        self.assertTrue(result.free_shipping)
        self.assertEqual(result.shipping_discount, base_quote.cost)
        self.assertEqual(user_coupon.coupon_id, reward.coupon_id)   # همان کد بازخریدشده

    def test_the_same_code_is_rejected_before_redemption_not_assigned(self):
        """ پیش از بازخرید، هیچ UserCoupon ای وجود ندارد؛ audience=assigned باید صریحاً رد کند. """
        reward = _make_free_shipping_reward(points_cost=100)   # عمداً بازخرید نمی‌شود

        base_quote = self._courier_quote(cost=45000)
        result = evaluate_coupon(reward.coupon, self.user, self._pricing(), base_quote=base_quote, now=timezone.now())

        self.assertFalse(result.ok)
        self.assertEqual(result.error, 'not_assigned')
        self.assertFalse(UserCoupon.objects.filter(coupon=reward.coupon, user=self.user).exists())
