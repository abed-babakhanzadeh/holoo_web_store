"""
تست‌های Loyalty Phase 5D-2: گزارش مالی و حسابرسی تعهدات لجر
(loyalty/reports.py::build_financial_report + LoyaltyAccountAdmin.report_view).
"""

import itertools

from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from accounts.models import CustomUser
from loyalty import reports, services
from loyalty.models import LoyaltyTransaction
from products.models import SiteSettings

_seq = itertools.count(1)

REPORT_URL = 'admin:loyalty_loyaltyaccount_report'


def _make_user(**extra):
    return CustomUser.objects.create_user(phone_number=f'0912080{next(_seq):04d}', **extra)


class SiteSettingsTestBase(TestCase):
    def setUp(self):
        super().setUp()
        cache.delete(SiteSettings.CACHE_KEY)
        SiteSettings.load().save()
        self.addCleanup(self._reset_settings)

    def _reset_settings(self):
        obj = SiteSettings.load()
        obj.loyalty_redeem_toman_per_point = 100
        obj.save()
        cache.delete(SiteSettings.CACHE_KEY)


class ReportViewSecurityTests(SiteSettingsTestBase):
    """ سناریوی ۱: دسترسی و امنیت ویوی گزارش. """

    def test_anonymous_user_is_redirected_to_admin_login(self):
        response = self.client.get(reverse(REPORT_URL))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/login/', response.url)

    def test_authenticated_non_staff_user_is_redirected_to_admin_login(self):
        user = _make_user()
        self.client.force_login(user)
        response = self.client.get(reverse(REPORT_URL))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/login/', response.url)

    def test_staff_user_without_view_permission_is_denied(self):
        user = _make_user(is_staff=True)
        self.client.force_login(user)
        response = self.client.get(reverse(REPORT_URL))
        self.assertEqual(response.status_code, 403)

    def test_staff_user_with_view_permission_can_access(self):
        user = _make_user(is_staff=True)
        perm = Permission.objects.get(codename='view_loyaltyaccount', content_type__app_label='loyalty')
        user.user_permissions.add(perm)
        self.client.force_login(user)

        response = self.client.get(reverse(REPORT_URL))

        self.assertEqual(response.status_code, 200)
        self.assertIn('report', response.context)


class EmptyDatabaseReportTests(SiteSettingsTestBase):
    """ سناریوی ۲: پایگاه‌داده‌ی کاملاً خالی - همه‌چیز صفر، بدون ZeroDivisionError. """

    def test_report_with_no_accounts_returns_all_zeros(self):
        report = reports.build_financial_report()

        self.assertEqual(report['outstanding_points'], 0)
        self.assertEqual(report['lifetime_earned'], 0)
        self.assertEqual(report['lifetime_redeemed'], 0)
        self.assertEqual(report['burn_to_earn_ratio'], 0)
        self.assertEqual(report['points_liability_toman'], 0)
        self.assertEqual(len(report['channels']), 3)
        for row in report['channels']:
            self.assertEqual(row['amount'], 0)


class RealDataAggregationTests(SiteSettingsTestBase):
    """ سناریوی ۳: صحت محاسبات تجمیعی با داده‌ی واقعی (کسب، هر سه کانال کسر). """

    def test_aggregation_matches_the_real_ledger(self):
        settings_obj = SiteSettings.load()
        settings_obj.loyalty_redeem_toman_per_point = 100
        settings_obj.save()
        cache.delete(SiteSettings.CACHE_KEY)

        user_a = _make_user()
        user_b = _make_user()

        services.credit_points(user_a, 500, LoyaltyTransaction.EARN_ORDER, 'کسب ۱')
        services.credit_points(user_b, 300, LoyaltyTransaction.EARN_ORDER, 'کسب ۲')

        services.debit_points(user_a, 100, LoyaltyTransaction.REDEEM_WALLET, 'تبدیل به کیف‌پول')
        services.debit_points(user_a, 50, LoyaltyTransaction.REDEEM_REWARD, 'بازخرید پاداش')
        services.debit_points(user_b, 20, LoyaltyTransaction.ADMIN_DEBIT, 'اصلاحیه ادمین')

        report = reports.build_financial_report()

        # lifetime_earned = 500+300=800 ؛ lifetime_redeemed = 100+50+20=170 ؛ outstanding = 800-170=630
        self.assertEqual(report['lifetime_earned'], 800)
        self.assertEqual(report['lifetime_redeemed'], 170)
        self.assertEqual(report['outstanding_points'], 630)
        self.assertEqual(report['burn_to_earn_ratio'], round(170 * 100 / 800, 1))
        self.assertEqual(report['points_liability_toman'], 630 * 100)

        channel_amounts = {row['key']: row['amount'] for row in report['channels']}
        self.assertEqual(channel_amounts[LoyaltyTransaction.REDEEM_WALLET], 100)
        self.assertEqual(channel_amounts[LoyaltyTransaction.REDEEM_REWARD], 50)
        self.assertEqual(channel_amounts[LoyaltyTransaction.ADMIN_DEBIT], 20)
        # همه‌ی مقادیر مثبت‌اند - نه منفی خامِ amount ذخیره‌شده در دفترکل
        for amount in channel_amounts.values():
            self.assertGreaterEqual(amount, 0)
