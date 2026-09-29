"""
تست‌های Loyalty Phase 4D-2: سطح وب باشگاه مشتریان (loyalty/views.py، loyalty/forms.py).

عمداً هیچ‌کدام از این تست‌ها هسته‌ی سرویس‌ها (loyalty/redemption.py، loyalty/reward_redemption.py،
loyalty/services.py) را مستقیم نمی‌سنجند - آن‌ها قبلاً در tests_redemption.py/tests_reward_redemption.py
تست شده‌اند. اینجا فقط لایه‌ی وب (احراز هویت، فرم، نگاشت استثنا به پیام، PRG/HTMX، ایدمپوتنسی سطح وب) سنجیده می‌شود.
"""

import itertools

from django.contrib.messages import get_messages
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomUser
from loyalty import services
from loyalty.models import LoyaltyAccount, LoyaltyReward, LoyaltyTier, LoyaltyTransaction
from loyalty.progression import credit_points_with_progression
from products.models import SiteSettings
from promotions.models import Coupon, UserCoupon
from promotions.testing import make_coupon

_seq = itertools.count(1)

DASHBOARD = 'loyalty:dashboard'
REWARDS = 'loyalty:rewards'
REDEEM_WALLET = 'loyalty:redeem_wallet'


def _make_user():
    return CustomUser.objects.create_user(phone_number=f'0912074{next(_seq):04d}')


def _make_reward(points_cost=100, is_active=True, **coupon_fields):
    """ is_active اینجا فقط روی خودِ LoyaltyReward اعمال می‌شود؛ چون Coupon هم فیلدی به همین نام دارد،
    عمداً پارامتر جداگانه است تا با **coupon_fields قاطی نشود (نمونه‌ای واقعی از این خطا). """
    coupon = make_coupon(f'WEB-REWARD-{next(_seq)}', audience=Coupon.AUDIENCE_ASSIGNED, **coupon_fields)
    return LoyaltyReward.objects.create(title=f'پاداش وب {next(_seq)}', coupon=coupon, points_cost=points_cost, is_active=is_active)


class LoyaltyWebTestBase(TestCase):
    def setUp(self):
        super().setUp()
        cache.delete(SiteSettings.CACHE_KEY)
        self._activate_club()
        self.addCleanup(self._reset_settings)

    def _activate_club(self):
        obj = SiteSettings.load()
        obj.loyalty_activated_at = timezone.now() - timezone.timedelta(days=1)
        obj.save()
        cache.delete(SiteSettings.CACHE_KEY)

    def _deactivate_club(self):
        obj = SiteSettings.load()
        obj.loyalty_activated_at = None
        obj.save()
        cache.delete(SiteSettings.CACHE_KEY)

    def _reset_settings(self):
        obj = SiteSettings.load()
        obj.loyalty_activated_at = None
        obj.save()
        cache.delete(SiteSettings.CACHE_KEY)

    def _give_points(self, user, amount):
        return services.credit_points(user, amount, LoyaltyTransaction.EARN_ORDER, 'کسب تست')

    def _login(self, user):
        self.client.force_login(user)

    def _messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]


class LoginRequiredTests(LoyaltyWebTestBase):
    """ هر ۴ صفحه باید کاربر احراز هویت‌نشده را به صفحه‌ی ورود هدایت کنند. """

    def test_dashboard_requires_login(self):
        response = self.client.get(reverse(DASHBOARD))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith('/accounts/login/'))

    def test_rewards_requires_login(self):
        response = self.client.get(reverse(REWARDS))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith('/accounts/login/'))

    def test_redeem_wallet_requires_login(self):
        response = self.client.get(reverse(REDEEM_WALLET))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith('/accounts/login/'))

    def test_redeem_reward_requires_login(self):
        response = self.client.post(reverse('loyalty:redeem_reward', args=[1]))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith('/accounts/login/'))


class DashboardViewTests(LoyaltyWebTestBase):
    def test_dashboard_loads_for_a_user_without_a_loyalty_account(self):
        user = _make_user()
        self._login(user)
        response = self.client.get(reverse(DASHBOARD))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['current_balance'], 0)
        self.assertContains(response, 'باشگاه مشتریان')

    def test_dashboard_shows_balances_and_paginated_history(self):
        user = _make_user()
        self._give_points(user, 300)
        self._login(user)
        response = self.client.get(reverse(DASHBOARD))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['current_balance'], 300)
        self.assertEqual(response.context['lifetime_earned'], 300)
        self.assertIn('page_obj', response.context)

    def test_dashboard_shows_inactive_club_message_when_not_activated(self):
        user = _make_user()
        self._deactivate_club()
        self._login(user)
        response = self.client.get(reverse(DASHBOARD))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'به‌زودی فعال می‌شود')

    def test_no_upgrade_modal_when_there_is_no_tier_history(self):
        user = _make_user()
        self._give_points(user, 300)   # کسب معمولی، بدون هیچ LoyaltyTier ای تعریف‌شده - بدون ارتقا
        self._login(user)
        response = self.client.get(reverse(DASHBOARD))
        self.assertIsNone(response.context['newly_upgraded_tier'])

    def test_tier_upgrade_modal_shown_once_then_not_repeated(self):
        """ سناریوی سشن ۶B: مودال فقط در اولین بازدید بعد از ارتقا نشان داده می‌شود، نه در بازدیدهای بعدی. """
        LoyaltyTier.objects.all().delete()   # پاک‌سازی Seed فاز ۳C؛ نیازی به addCleanup نیست - TestCase خودش کل تراکنش را rollback می‌کند
        tier = LoyaltyTier.objects.create(title='برنزی وب', rank=0, threshold=100)

        user = _make_user()
        credit_points_with_progression(user, 100, LoyaltyTransaction.EARN_ORDER, 'کسب تست ۶B')
        self._login(user)

        first = self.client.get(reverse(DASHBOARD))
        self.assertEqual(first.context['newly_upgraded_tier'], tier)
        self.assertContains(first, 'تبریک! سطح شما ارتقا یافت')
        self.assertContains(first, 'برنزی وب')

        second = self.client.get(reverse(DASHBOARD))
        self.assertIsNone(second.context['newly_upgraded_tier'])
        self.assertNotContains(second, 'تبریک! سطح شما ارتقا یافت')


class RedeemToWalletViewTests(LoyaltyWebTestBase):
    def _post(self, points, token='tok-1'):
        return self.client.post(reverse(REDEEM_WALLET), {'points': points, 'idempotency_token': token})

    def test_successful_redemption_prg(self):
        user = _make_user()
        self._give_points(user, 300)
        self._login(user)

        response = self._post(100, token='wallet-tok-1')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse(REDEEM_WALLET))
        self.assertTrue(any('تبدیل شد' in m for m in self._messages(response)))

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 200)

    def test_successful_redemption_htmx(self):
        user = _make_user()
        self._give_points(user, 300)
        self._login(user)

        response = self.client.post(
            reverse(REDEEM_WALLET), {'points': 100, 'idempotency_token': 'wallet-tok-htmx'},
            HTTP_HX_REQUEST='true',
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'loyalty/partials/redeem_result.html')
        self.assertContains(response, 'تبدیل شد')

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 200)

    def test_insufficient_points(self):
        user = _make_user()
        self._give_points(user, 50)
        self._login(user)

        response = self._post(100, token='wallet-tok-2')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(any('کافی نیست' in m for m in self._messages(response)))
        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 50)

    def test_invalid_points_format(self):
        user = _make_user()
        self._give_points(user, 300)
        self._login(user)

        response = self.client.post(reverse(REDEEM_WALLET), {'points': 'abc', 'idempotency_token': 'wallet-tok-3'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(any('نامعتبر' in m for m in self._messages(response)))
        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 300)   # هیچ کسری اعمال نشد

    def test_idempotency_conflict_shows_friendly_message_not_a_technical_error(self):
        user = _make_user()
        self._give_points(user, 500)
        self._login(user)

        self._post(100, token='wallet-tok-conflict')
        response = self._post(200, token='wallet-tok-conflict')   # همان توکن، مقدار متفاوت
        self.assertEqual(response.status_code, 302)
        page_messages = self._messages(response)
        self.assertTrue(any('قبلاً ارسال شده' in m for m in page_messages))
        self.assertFalse(any('IdempotencyKeyConflictError' in m for m in page_messages))

    def test_redemption_rejected_when_club_not_activated(self):
        user = _make_user()
        self._give_points(user, 300)
        self._deactivate_club()
        self._login(user)

        response = self._post(100, token='wallet-tok-inactive-club')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(any('فعال نشده' in m for m in self._messages(response)))
        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 300)


class RewardCatalogViewTests(LoyaltyWebTestBase):
    def test_shows_active_rewards(self):
        user = _make_user()
        _make_reward(points_cost=100)
        self._login(user)

        response = self.client.get(reverse(REWARDS))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['rewards']), 1)

    def test_inactive_rewards_are_excluded(self):
        user = _make_user()
        _make_reward(points_cost=100)
        LoyaltyReward.objects.create(
            title='پاداش غیرفعال', coupon=make_coupon('WEB-INACTIVE', audience=Coupon.AUDIENCE_ASSIGNED),
            points_cost=50, is_active=False,
        )
        self._login(user)

        response = self.client.get(reverse(REWARDS))
        self.assertEqual(len(response.context['rewards']), 1)

    def test_empty_state_when_no_active_rewards(self):
        user = _make_user()
        self._login(user)
        response = self.client.get(reverse(REWARDS))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'پاداشی برای بازخرید وجود ندارد')


class RedeemRewardViewTests(LoyaltyWebTestBase):
    def _url(self, reward):
        return reverse('loyalty:redeem_reward', args=[reward.pk])

    def test_get_is_not_allowed(self):
        user = _make_user()
        reward = _make_reward(points_cost=100)
        self._login(user)
        response = self.client.get(self._url(reward))
        self.assertEqual(response.status_code, 405)

    def test_successful_redemption(self):
        user = _make_user()
        self._give_points(user, 300)
        reward = _make_reward(points_cost=100)
        self._login(user)

        response = self.client.post(self._url(reward), {'idempotency_token': 'reward-tok-1'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse(REWARDS))
        self.assertTrue(any('دریافت شد' in m for m in self._messages(response)))

        self.assertTrue(UserCoupon.objects.filter(coupon=reward.coupon, user=user, source=UserCoupon.SOURCE_AUTO).exists())
        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 200)

    def test_replay_with_the_same_token_does_not_double_debit(self):
        user = _make_user()
        self._give_points(user, 300)
        reward = _make_reward(points_cost=100)
        self._login(user)

        self.client.post(self._url(reward), {'idempotency_token': 'reward-tok-replay'})
        self.client.post(self._url(reward), {'idempotency_token': 'reward-tok-replay'})   # همان توکن دوباره

        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 200)   # فقط یک‌بار کسر شد
        self.assertEqual(UserCoupon.objects.filter(coupon=reward.coupon, user=user).count(), 1)

    def test_inactive_reward_returns_404(self):
        user = _make_user()
        reward = _make_reward(points_cost=100, is_active=False)   # هم‌الگوی create مستقیم
        self._login(user)
        response = self.client.post(self._url(reward), {'idempotency_token': 'reward-tok-inactive'})
        self.assertEqual(response.status_code, 404)

    def test_out_of_stock_reward(self):
        user = _make_user()
        self._give_points(user, 300)
        reward = _make_reward(points_cost=100, claim_limit=1)
        other_user = _make_user()
        UserCoupon.objects.create(coupon=reward.coupon, user=other_user, source=UserCoupon.SOURCE_AUTO)
        self._login(user)

        response = self.client.post(self._url(reward), {'idempotency_token': 'reward-tok-full'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(any('تکمیل شده' in m for m in self._messages(response)))
        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 300)

    def test_already_redeemed_reward_with_a_different_token(self):
        user = _make_user()
        self._give_points(user, 300)
        reward = _make_reward(points_cost=100)
        self._login(user)

        self.client.post(self._url(reward), {'idempotency_token': 'reward-tok-first'})
        response = self.client.post(self._url(reward), {'idempotency_token': 'reward-tok-second'})

        self.assertEqual(response.status_code, 302)
        self.assertTrue(any('قبلاً این پاداش را دریافت' in m for m in self._messages(response)))
        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 200)   # فقط بار اول کسر شد

    def test_reward_redemption_rejected_when_club_not_activated(self):
        user = _make_user()
        self._give_points(user, 300)
        reward = _make_reward(points_cost=100)
        self._deactivate_club()
        self._login(user)

        response = self.client.post(self._url(reward), {'idempotency_token': 'reward-tok-club'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(any('فعال نشده' in m for m in self._messages(response)))
        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 300)

    def test_successful_redemption_htmx(self):
        user = _make_user()
        self._give_points(user, 300)
        reward = _make_reward(points_cost=100)
        self._login(user)

        response = self.client.post(
            self._url(reward), {'idempotency_token': 'reward-tok-htmx'}, HTTP_HX_REQUEST='true',
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'loyalty/partials/redeem_result.html')
        self.assertContains(response, 'دریافت شد')
