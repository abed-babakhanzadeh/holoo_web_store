"""
تست‌های Loyalty Phase 3D-2B: هم‌زیستی دوگانه‌ی سطح کلاسیک/باشگاه جدید در پیشخوان
(accounts/views.py::DashboardView + templates/accounts/dashboard.html).

عمداً هیچ منطقی از promotions/pricing/سبد/چک‌اوت یا هسته‌ی لجر (loyalty/earning.py،
cancellation.py، returns.py، services.py) اینجا تغییر/تست نمی‌شود - فقط لایه‌ی نمایش.
"""

import itertools

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomUser
from loyalty import services
from loyalty.models import LoyaltyAccount, LoyaltyTier, LoyaltyTransaction
from products.models import SiteSettings

_seq = itertools.count(1)


def _make_user():
    return CustomUser.objects.create_user(phone_number=f'0912090{next(_seq):04d}')


class LoyaltyDashboardDisplayTestBase(TestCase):
    def setUp(self):
        super().setUp()
        cache.delete(SiteSettings.CACHE_KEY)
        SiteSettings.load().save()
        self.addCleanup(self._reset_settings)

    def _reset_settings(self):
        obj = SiteSettings.load()
        obj.loyalty_activated_at = None
        obj.save()
        cache.delete(SiteSettings.CACHE_KEY)

    def _login(self, user):
        self.client.force_login(user)


class ClassicOnlyUserDashboardTests(LoyaltyDashboardDisplayTestBase):
    """ کاربری که فقط سابقه‌ی سنتی دارد (بدون LoyaltyAccount) - باشگاه جدید هم فعال نشده. """

    def test_dashboard_renders_without_error_and_shows_both_sections(self):
        user = _make_user()
        self._login(user)

        response = self.client.get(reverse('accounts:dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'سطح کلاسیک')
        self.assertContains(response, 'باشگاه مشتریان جدید')
        self.assertContains(response, 'به‌زودی فعال می‌شود')   # چون loyalty_activated_at=None

    def test_context_has_safe_defaults_for_a_user_without_a_loyalty_account(self):
        """
        خودبسنده (Self-Contained): به‌جای تکیه بر ۵ سطح Seed‌شده‌ی محیطی (که با --keepdb، وقتی
        یک TransactionTestCase در اپ loyalty پیش‌تر در همان اجرا دیتابیس تست را flush کرده،
        ممکن است غایب باشند - نگاه کنید گزارش کنترل نهایی محدوده‌ی فاز ۳D-2B)، این تست دقیقاً
        همان یک سطح پایه‌ی لازم را خودش می‌سازد؛ صرف‌نظر از وضعیت پیشینِ دیتابیس تست، نتیجه
        همیشه یکسان و قابل‌پیش‌بینی است.
        """
        LoyaltyTier.objects.all().delete()
        base_tier = LoyaltyTier.objects.create(title='مشتری پایه', rank=0, threshold=0, badge_color='#9CA3AF')
        self.addCleanup(LoyaltyTier.objects.all().delete)

        user = _make_user()
        self.assertFalse(LoyaltyAccount.objects.filter(user=user).exists())
        self._login(user)

        response = self.client.get(reverse('accounts:dashboard'))

        self.assertEqual(response.context['dynamic_tier'], base_tier)
        self.assertEqual(response.context['loyalty_current_balance'], 0)
        self.assertEqual(response.context['loyalty_lifetime_earned'], 0)
        self.assertFalse(response.context['loyalty_club_activated'])
        # سیستم سنتی همچنان دقیقاً همان مقادیر قبلی را می‌دهد
        self.assertEqual(response.context['loyalty_points'], user.get_loyalty_points())


class ClassicAndLedgerUserDashboardTests(LoyaltyDashboardDisplayTestBase):
    """ کاربری که هم سابقه‌ی سنتی دارد هم امتیاز باشگاه جدید (لجر). """

    def setUp(self):
        super().setUp()
        LoyaltyTier.objects.all().delete()
        self.base = LoyaltyTier.objects.create(title='مشتری پایه', rank=0, threshold=0, badge_color='#9CA3AF')
        self.bronze = LoyaltyTier.objects.create(title='برنزی', rank=1, threshold=200, badge_color='#CD7F32')
        self.addCleanup(LoyaltyTier.objects.all().delete)

        settings_obj = SiteSettings.load()
        settings_obj.loyalty_activated_at = timezone.now() - timezone.timedelta(days=1)
        settings_obj.save()

    def test_dashboard_shows_dynamic_tier_badge_and_balances(self):
        user = _make_user()
        services.credit_points(user, 250, LoyaltyTransaction.EARN_ORDER, 'کسب تست')
        self._login(user)

        response = self.client.get(reverse('accounts:dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['dynamic_tier'], self.bronze)
        self.assertEqual(response.context['loyalty_current_balance'], 250)
        self.assertEqual(response.context['loyalty_lifetime_earned'], 250)
        self.assertTrue(response.context['loyalty_club_activated'])
        self.assertContains(response, 'برنزی')
        self.assertContains(response, '250')
        self.assertNotContains(response, 'به‌زودی فعال می‌شود')

    def test_spending_does_not_change_the_displayed_tier_or_lifetime_earned(self):
        user = _make_user()
        services.credit_points(user, 250, LoyaltyTransaction.EARN_ORDER, 'کسب تست')
        services.debit_points(user, 100, LoyaltyTransaction.REDEEM_WALLET, 'خرج تست')
        self._login(user)

        response = self.client.get(reverse('accounts:dashboard'))

        self.assertEqual(response.context['dynamic_tier'], self.bronze)   # هنوز برنزی
        self.assertEqual(response.context['loyalty_current_balance'], 150)   # موجودیِ قابل‌خرج کم شده
        self.assertEqual(response.context['loyalty_lifetime_earned'], 250)   # دست‌نخورده


class DashboardIsPureReadOnlyTests(LoyaltyDashboardDisplayTestBase):
    def test_viewing_the_dashboard_creates_no_loyalty_records(self):
        user = _make_user()
        self._login(user)

        accounts_before = LoyaltyAccount.objects.count()
        transactions_before = LoyaltyTransaction.objects.count()

        for _ in range(3):   # چند بار بازدید - همچنان بدون Side-Effect
            response = self.client.get(reverse('accounts:dashboard'))
            self.assertEqual(response.status_code, 200)

        self.assertEqual(LoyaltyAccount.objects.count(), accounts_before)
        self.assertEqual(LoyaltyTransaction.objects.count(), transactions_before)
        self.assertFalse(LoyaltyAccount.objects.filter(user=user).exists())


class DashboardOtherSectionsRegressionTests(LoyaltyDashboardDisplayTestBase):
    """ اثبات این‌که بقیه‌ی بخش‌های پیشخوان (کیف‌پول، سفارش‌های در انتظار، علاقه‌مندی‌ها) دست‌نخورده‌اند. """

    def test_wallet_pending_and_favorites_cards_still_render(self):
        user = _make_user()
        self._login(user)

        response = self.client.get(reverse('accounts:dashboard'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'موجودی کیف پول')
        self.assertContains(response, 'سفارش در انتظار پرداخت')
        self.assertContains(response, 'تعداد علاقه‌مندی‌ها')
        self.assertEqual(response.context['pending_orders_count'], 0)
        self.assertEqual(response.context['favorites_count'], 0)

    def test_classic_loyalty_context_keys_are_unchanged(self):
        """ کلیدهای سیستم سنتی که از قبل وجود داشتند، عیناً همچنان در context هستند. """
        user = _make_user()
        self._login(user)

        response = self.client.get(reverse('accounts:dashboard'))

        for key in ('loyalty_points', 'loyalty_level', 'loyalty_next_level', 'loyalty_remaining', 'loyalty_progress_percent'):
            self.assertIn(key, response.context)
