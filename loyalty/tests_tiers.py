"""
تست‌های Loyalty Phase 3A/3C: هسته‌ی مدل سطوح داینامیک (LoyaltyTier) + Seed داده‌ای + ادمین.

عمداً هیچ اتصالی به accounts.models.CustomUser/promotions/تمپلیت‌ها تست نمی‌شود - این فاز فقط
مدل، قیود، اعتبارسنجی cross-row، تابع کمکی خواندنی get_tier_for_lifetime_points، مایگریشن
Seed و پنل ادمین را می‌سازد.

نکته‌ی مهم درباره‌ی داده‌ی پایه: از فاز ۳C به بعد، مایگریشن Seed (loyalty/migrations/
0003_seed_loyalty_tiers.py) همین که پایگاه‌داده‌ی تست ساخته می‌شود اجرا و ۵ سطح واقعی
(rank=0..4) را ثبت می‌کند - یعنی این ۵ ردیف در *همه‌ی* تست‌های این فایل از قبل حاضرند (چون
migrate یک‌بار قبل از هر تراکنش تست اجرا می‌شود، نه داخل آن). تست‌های فاز ۳A که فرض «جدول
خالی» داشتند حالا از IsolatedTierTestCase ارث می‌برند که همان ۵ ردیف را در setUp پاک می‌کند؛
تست‌های خودِ Seed (SeedMigrationDataTests) عمداً این پاک‌سازی را ندارند تا دقیقاً همان چیزی را
بسنجند که مایگریشن واقعاً ساخته.
"""

import importlib.util
import os

from django.apps import apps as django_apps
from django.contrib import admin as django_admin
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from accounts.models import CustomUser

from . import services
from .exceptions import LoyaltyTierDeletionError
from .models import LoyaltyAccount, LoyaltyTier, LoyaltyTransaction

SEED_MIGRATION_PATH = os.path.join(os.path.dirname(__file__), 'migrations', '0003_seed_loyalty_tiers.py')


def _load_seed_migration_module():
    """ ماژول مایگریشن Seed را مستقیم از مسیر فایل import می‌کند - برای تست idempotency بدون نیاز به migrate واقعی. """
    spec = importlib.util.spec_from_file_location('loyalty_seed_migration_0003', SEED_MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_tier(title, rank, threshold, **overrides):
    data = dict(title=title, rank=rank, threshold=threshold)
    data.update(overrides)
    return LoyaltyTier.objects.create(**data)


class IsolatedTierTestCase(TestCase):
    """ هر تستی که از این ارث ببرد، با جدول کاملاً خالی از LoyaltyTier شروع می‌کند (Seed فاز ۳C را هم پاک می‌کند). """

    def setUp(self):
        super().setUp()
        LoyaltyTier.objects.all().delete()   # bulk QuerySet.delete() - از delete() نمونه‌ای/مسدودشده عبور نمی‌کند و مجاز است


class TierCreationTests(IsolatedTierTestCase):
    def test_creating_tiers_with_ascending_rank_and_threshold_succeeds(self):
        bronze = _make_tier('برنزی', 1, 100)
        silver = _make_tier('نقره‌ای', 2, 500)
        gold = _make_tier('طلایی', 3, 1500)

        self.assertEqual(list(LoyaltyTier.objects.all()), [bronze, silver, gold])   # ordering = ('rank',)
        self.assertTrue(bronze.is_active)
        self.assertEqual(bronze.badge_color, '')

    def test_str_representation(self):
        tier = _make_tier('برنزی', 1, 100)
        self.assertEqual(str(tier), 'برنزی (رتبه 1)')

    def test_badge_color_and_is_active_are_settable(self):
        tier = _make_tier('طلایی', 1, 100, badge_color='#FFD700', is_active=False)
        self.assertEqual(tier.badge_color, '#FFD700')
        self.assertFalse(tier.is_active)


class TierUniquenessTests(IsolatedTierTestCase):
    def setUp(self):
        super().setUp()
        _make_tier('برنزی', 1, 100)

    def test_duplicate_title_is_rejected_at_database_level(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            _make_tier('برنزی', 2, 999)

    def test_duplicate_rank_is_rejected_at_database_level(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            _make_tier('نقره‌ای', 1, 999)

    def test_duplicate_threshold_is_rejected_at_database_level(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            _make_tier('نقره‌ای', 2, 100)


class TierCleanValidationTests(IsolatedTierTestCase):
    """ اعتبارسنجی بین‌ردیفی (Cross-Row) در LoyaltyTier.clean(). """

    def setUp(self):
        super().setUp()
        self.bronze = _make_tier('برنزی', 1, 1000)

    def test_higher_rank_with_lower_threshold_is_rejected(self):
        """ سناریوی دقیق دستورالعمل: رتبه ۱ با آستانه ۱۰۰۰، رتبه ۲ با آستانه ۵۰۰ (نامعتبر). """
        silver = LoyaltyTier(title='نقره‌ای', rank=2, threshold=500)
        with self.assertRaises(ValidationError) as caught:
            silver.full_clean()
        self.assertIn('threshold', caught.exception.message_dict)

    def test_higher_rank_with_equal_threshold_is_rejected(self):
        """ صعودیِ اکید یعنی تساوی هم مجاز نیست. """
        silver = LoyaltyTier(title='نقره‌ای', rank=2, threshold=1000)
        with self.assertRaises(ValidationError):
            silver.full_clean()

    def test_higher_rank_with_higher_threshold_is_accepted(self):
        silver = LoyaltyTier(title='نقره‌ای', rank=2, threshold=1500)
        silver.full_clean()   # نباید خطا بدهد
        silver.save()
        self.assertEqual(list(LoyaltyTier.objects.values_list('title', flat=True)), ['برنزی', 'نقره‌ای'])

    def test_lower_rank_with_higher_threshold_is_rejected(self):
        """ همان قاعده از سمت دیگر: رتبه‌ی پایین‌تر نباید آستانه‌ی بالاتر داشته باشد. """
        pending = LoyaltyTier(title='مشتری جدید', rank=0, threshold=2000)
        with self.assertRaises(ValidationError):
            pending.full_clean()

    def test_editing_an_existing_tier_is_validated_against_the_rest(self):
        """ clean() باید نسخه‌ی *جدید* خودِ رکورد را جایگزین قبلی کند، نه یک ردیف اضافه بشمارد. """
        gold = _make_tier('طلایی', 2, 2000)
        self.bronze.threshold = 2500   # حالا برنزی (رتبه ۱) از طلایی (رتبه ۲) بیشتر می‌شود - نامعتبر
        with self.assertRaises(ValidationError):
            self.bronze.full_clean()

    def test_editing_an_existing_tier_within_valid_range_succeeds(self):
        _make_tier('طلایی', 2, 2000)
        self.bronze.threshold = 1200   # هنوز کمتر از طلایی - معتبر
        self.bronze.full_clean()   # نباید خطا بدهد
        self.bronze.save()
        self.bronze.refresh_from_db()
        self.assertEqual(self.bronze.threshold, 1200)


class TierDeletionBlockedTests(IsolatedTierTestCase):
    def test_direct_delete_raises_domain_error(self):
        tier = _make_tier('برنزی', 1, 100)
        with self.assertRaises(LoyaltyTierDeletionError):
            tier.delete()
        self.assertTrue(LoyaltyTier.objects.filter(pk=tier.pk).exists())

    def test_soft_deactivation_is_the_supported_path(self):
        tier = _make_tier('برنزی', 1, 100)
        tier.is_active = False
        tier.save(update_fields=['is_active'])
        tier.refresh_from_db()
        self.assertFalse(tier.is_active)
        self.assertTrue(LoyaltyTier.objects.filter(pk=tier.pk).exists())   # همچنان در دیتابیس


class GetTierForLifetimePointsTests(IsolatedTierTestCase):
    """ loyalty/services.py::get_tier_for_lifetime_points (تابع کمکی خواندنیِ فاز ۳A). """

    def setUp(self):
        super().setUp()
        self.bronze = _make_tier('برنزی', 1, 100)
        self.silver = _make_tier('نقره‌ای', 2, 500)
        self.gold = _make_tier('طلایی', 3, 1500)

    def test_points_below_lowest_threshold_returns_none(self):
        self.assertIsNone(services.get_tier_for_lifetime_points(50))

    def test_points_exactly_at_a_threshold_returns_that_tier(self):
        self.assertEqual(services.get_tier_for_lifetime_points(100), self.bronze)
        self.assertEqual(services.get_tier_for_lifetime_points(500), self.silver)

    def test_points_between_two_thresholds_returns_the_lower_tier(self):
        self.assertEqual(services.get_tier_for_lifetime_points(499), self.bronze)
        self.assertEqual(services.get_tier_for_lifetime_points(1499), self.silver)

    def test_points_above_highest_threshold_returns_the_highest_tier(self):
        self.assertEqual(services.get_tier_for_lifetime_points(999999), self.gold)

    def test_zero_points_returns_none_when_lowest_threshold_is_positive(self):
        self.assertIsNone(services.get_tier_for_lifetime_points(0))

    def test_inactive_tier_is_never_returned(self):
        self.gold.is_active = False
        self.gold.save(update_fields=['is_active'])
        self.assertEqual(services.get_tier_for_lifetime_points(999999), self.silver)   # نه طلایی، چون غیرفعال است

    def test_no_tiers_defined_returns_none(self):
        LoyaltyTier.objects.all().delete()
        self.assertIsNone(services.get_tier_for_lifetime_points(100000))


# ============================================================================== Phase 3C: صحت داده‌ی Seed واقعی
class SeedMigrationDataTests(IsolatedTierTestCase):
    """
    محتوای واقعیِ خروجیِ مایگریشن Seed را می‌سنجد - اما به‌جای تکیه بر اینکه آن ۵ ردیف هنوز از
    لحظه‌ی ساخت پایگاه‌داده‌ی تست دست‌نخورده مانده باشند (که با --keepdb و وجود چند
    TransactionTestCase در همین اپ که هر کدام کل دیتابیس را flush می‌کنند تضمین‌شدنی نیست -
    دقیقاً همان تله‌ی مستندشده در holoo-env-gotchas برای NotificationSetting)، در setUp خودش
    (بعد از پاک‌سازی IsolatedTierTestCase) مستقیماً همان تابع مایگریشن را صدا می‌زند تا این تست
    مستقل از ترتیب/فلاش سایر تست‌ها همیشه قابل‌اتکا باشد.
    """

    def setUp(self):
        super().setUp()
        _load_seed_migration_module().seed_loyalty_tiers(django_apps, None)

    EXPECTED = [
        {'rank': 0, 'title': 'مشتری پایه', 'threshold': 0, 'badge_color': '#9CA3AF'},
        {'rank': 1, 'title': 'برنزی', 'threshold': 200, 'badge_color': '#CD7F32'},
        {'rank': 2, 'title': 'نقره‌ای', 'threshold': 500, 'badge_color': '#C0C0C0'},
        {'rank': 3, 'title': 'طلایی', 'threshold': 1200, 'badge_color': '#FFD700'},
        {'rank': 4, 'title': 'الماسی', 'threshold': 2500, 'badge_color': '#38BDF8'},
    ]

    def test_exactly_five_tiers_are_seeded(self):
        self.assertEqual(LoyaltyTier.objects.count(), 5)

    def test_seeded_values_match_the_approved_table_exactly(self):
        for expected in self.EXPECTED:
            with self.subTest(rank=expected['rank']):
                tier = LoyaltyTier.objects.get(rank=expected['rank'])
                self.assertEqual(tier.title, expected['title'])
                self.assertEqual(tier.threshold, expected['threshold'])
                self.assertEqual(tier.badge_color, expected['badge_color'])
                self.assertTrue(tier.is_active)

    def test_seeded_thresholds_are_not_copied_from_site_settings(self):
        """ تأیید صریح تصمیم فاز ۳B: آستانه‌ها مستقل‌اند، نه بازتاب ۳۰۰/۷۰۰/۱۵۰۰/۳۰۰۰ سیستم زنده‌ی قدیمی. """
        old_thresholds = {300, 700, 1500, 3000}
        new_thresholds = set(LoyaltyTier.objects.values_list('threshold', flat=True))
        self.assertEqual(new_thresholds, {0, 200, 500, 1200, 2500})
        self.assertNotEqual(new_thresholds, old_thresholds)


class SeedMigrationIdempotencyTests(IsolatedTierTestCase):
    """ فراخوانی مستقیم توابع RunPython مایگریشن - بدون نیاز به migrate واقعی روی دیتابیس تست. """

    def setUp(self):
        super().setUp()
        self.migration_module = _load_seed_migration_module()

    def test_running_seed_twice_creates_no_duplicates_and_raises_nothing(self):
        self.migration_module.seed_loyalty_tiers(django_apps, None)
        self.assertEqual(LoyaltyTier.objects.count(), 5)

        self.migration_module.seed_loyalty_tiers(django_apps, None)   # دومین اجرا - نباید IntegrityError بدهد
        self.assertEqual(LoyaltyTier.objects.count(), 5)   # همچنان ۵، نه ۱۰

    def test_rerunning_seed_does_not_overwrite_a_manual_admin_edit(self):
        self.migration_module.seed_loyalty_tiers(django_apps, None)
        bronze = LoyaltyTier.objects.get(rank=1)
        bronze.threshold = 250   # وانمود می‌کنیم ادمین دستی آستانه‌ی برنزی را تغییر داده
        bronze.save(update_fields=['threshold'])

        self.migration_module.seed_loyalty_tiers(django_apps, None)   # اجرای دوباره

        bronze.refresh_from_db()
        self.assertEqual(bronze.threshold, 250)   # دست‌نخورده مانده، نه بازنویسی‌شده به ۲۰۰

    def test_reverse_then_reseed_round_trip(self):
        self.migration_module.seed_loyalty_tiers(django_apps, None)
        self.assertEqual(LoyaltyTier.objects.count(), 5)

        self.migration_module.unseed_loyalty_tiers(django_apps, None)
        self.assertEqual(LoyaltyTier.objects.count(), 0)

        self.migration_module.seed_loyalty_tiers(django_apps, None)
        self.assertEqual(LoyaltyTier.objects.count(), 5)
        self.assertEqual(LoyaltyTier.objects.get(rank=1).threshold, 200)

    def test_reverse_does_not_delete_a_manually_edited_tier(self):
        self.migration_module.seed_loyalty_tiers(django_apps, None)
        bronze = LoyaltyTier.objects.get(rank=1)
        bronze.threshold = 250   # دیگر با مقادیر اصلیِ Seed مطابقت ندارد
        bronze.save(update_fields=['threshold'])

        self.migration_module.unseed_loyalty_tiers(django_apps, None)

        # ۴ سطح دیگر که هنوز دقیقاً با مقادیر اصلی مطابقت داشتند حذف شدند؛ فقط برنزیِ ویرایش‌شده باقی ماند
        self.assertEqual(LoyaltyTier.objects.count(), 1)
        self.assertTrue(LoyaltyTier.objects.filter(pk=bronze.pk).exists())
        bronze.refresh_from_db()
        self.assertEqual(bronze.threshold, 250)


# ============================================================================== Phase 3C: پنل ادمین
class LoyaltyTierAdminTests(IsolatedTierTestCase):
    def setUp(self):
        super().setUp()
        self.admin_user = CustomUser.objects.create_superuser(phone_number='09140099999')
        self.client.force_login(self.admin_user)

    def test_admin_is_registered(self):
        self.assertIn(LoyaltyTier, django_admin.site._registry)

    def test_changelist_shows_configured_columns(self):
        _make_tier('برنزی', 1, 100)
        response = self.client.get(reverse('admin:loyalty_loyaltytier_changelist'))
        self.assertEqual(response.status_code, 200)
        for expected_text in ('برنزی', '100'):
            self.assertContains(response, expected_text)

    def test_creating_a_new_tier_through_the_admin_form(self):
        response = self.client.post(reverse('admin:loyalty_loyaltytier_add'), {
            'title': 'پلاتینیوم', 'rank': '5', 'threshold': '5000',
            'badge_color': '#111111', 'is_active': 'on',
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(LoyaltyTier.objects.filter(title='پلاتینیوم', rank=5, threshold=5000).exists())

    def test_editing_an_existing_tier_through_the_admin_form(self):
        tier = _make_tier('برنزی', 1, 100)
        response = self.client.post(reverse('admin:loyalty_loyaltytier_change', args=[tier.pk]), {
            'title': 'برنزی', 'rank': '1', 'threshold': '150', 'badge_color': '', 'is_active': 'on',
        })
        self.assertEqual(response.status_code, 302)
        tier.refresh_from_db()
        self.assertEqual(tier.threshold, 150)

    def test_ascending_validation_is_enforced_through_the_admin_form(self):
        _make_tier('برنزی', 1, 1000)
        response = self.client.post(reverse('admin:loyalty_loyaltytier_add'), {
            'title': 'نقره‌ای', 'rank': '2', 'threshold': '500',   # نامعتبر: رتبه بالاتر، آستانه پایین‌تر
            'badge_color': '', 'is_active': 'on',
        })
        self.assertEqual(response.status_code, 200)   # فرم با خطا برمی‌گردد، ذخیره نمی‌شود
        self.assertFalse(LoyaltyTier.objects.filter(title='نقره‌ای').exists())

    def test_ascending_validation_is_enforced_through_list_editable(self):
        """ ویرایش سریع فهرست هم باید همان clean() را اجرا کند - نه یک مسیر جدا و بی‌قید. """
        bronze = _make_tier('برنزی', 1, 500)
        silver = _make_tier('نقره‌ای', 2, 1500)
        response = self.client.post(reverse('admin:loyalty_loyaltytier_changelist'), {
            'form-TOTAL_FORMS': '2', 'form-INITIAL_FORMS': '2', 'form-MIN_NUM_FORMS': '0', 'form-MAX_NUM_FORMS': '1000',
            'form-0-id': str(bronze.pk), 'form-0-rank': '1', 'form-0-threshold': '2000', 'form-0-is_active': 'on',
            'form-1-id': str(silver.pk), 'form-1-rank': '2', 'form-1-threshold': '1500', 'form-1-is_active': 'on',
            '_save': 'ذخیره',
        })
        self.assertEqual(response.status_code, 200)   # فرم‌ست با خطا برمی‌گردد (برنزی=۲۰۰۰ > نقره‌ای=۱۵۰۰)
        bronze.refresh_from_db()
        self.assertEqual(bronze.threshold, 500)   # تغییر اعمال نشد

    def test_physical_delete_is_blocked_in_the_admin(self):
        model_admin = django_admin.site._registry[LoyaltyTier]
        self.assertFalse(model_admin.has_delete_permission(None))

        tier = _make_tier('برنزی', 1, 100)
        response = self.client.get(reverse('admin:loyalty_loyaltytier_delete', args=[tier.pk]))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(LoyaltyTier.objects.filter(pk=tier.pk).exists())


# ============================================================================== عدم اختلال در فاز ۱/۲
class Phase1And2RegressionTests(TestCase):
    """
    اثبات می‌کند حضور مدل/مایگریشن/ادمین جدید LoyaltyTier هیچ رفتار موجود لجر (فاز ۱) یا
    Earn/Reverse (فاز ۲) را دست‌کاری نکرده - این‌ها فقط چند تست نماینده‌اند؛ پوشش کامل همان
    ۱۰۰ تست فاز ۱/۲ موجود است که جدا هم اجرا می‌شوند (نگاه کنید گزارش).
    """

    def test_credit_and_debit_points_are_unaffected_by_loyalty_tier_existing(self):
        from .exceptions import InsufficientPointsError
        from .models import LoyaltyAccount, LoyaltyTransaction

        user = CustomUser.objects.create_user(phone_number='09140088888')
        services.credit_points(user, 100, LoyaltyTransaction.EARN_ORDER, 'تست رگرسیون')
        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 100)
        self.assertEqual(account.lifetime_earned, 100)

        with self.assertRaises(InsufficientPointsError):
            services.debit_points(user, 200, LoyaltyTransaction.ADMIN_DEBIT, 'بیش از موجودی')


# ============================================================================== Phase 3D-1: سرویس خواندنی ارزیابی سطح کاربر
class GetDynamicTierForUserTests(IsolatedTierTestCase):
    """
    loyalty/services.py::get_dynamic_tier_for_user - آداپتور نازک بین LoyaltyAccount.lifetime_earned
    و get_tier_for_lifetime_points. عمداً هیچ کاری با accounts.models.CustomUser.get_loyalty_level_index
    (سیستم زنده‌ی قدیمی) ندارد و آن را صدا نمی‌زند.
    """

    def setUp(self):
        super().setUp()
        self.base = _make_tier('مشتری پایه', 0, 0)
        self.bronze = _make_tier('برنزی', 1, 200)
        self.silver = _make_tier('نقره‌ای', 2, 500)
        self.gold = _make_tier('طلایی', 3, 1200)

    def test_user_without_loyalty_account_evaluates_as_zero_points_without_creating_a_record(self):
        user = CustomUser.objects.create_user(phone_number='09140077001')
        self.assertFalse(LoyaltyAccount.objects.filter(user=user).exists())

        tier = services.get_dynamic_tier_for_user(user)

        self.assertEqual(tier, self.base)   # ۰ امتیاز -> سطح پایه (threshold=0)
        self.assertFalse(LoyaltyAccount.objects.filter(user=user).exists())   # هیچ رکوردی ساخته نشد

    def test_user_with_an_account_and_zero_lifetime_earned_gets_base_tier(self):
        user = CustomUser.objects.create_user(phone_number='09140077002')
        LoyaltyAccount.objects.create(user=user)   # lifetime_earned=0 پیش‌فرض

        tier = services.get_dynamic_tier_for_user(user)

        self.assertEqual(tier, self.base)

    def test_exact_boundary_at_threshold_returns_that_tier(self):
        user = CustomUser.objects.create_user(phone_number='09140077003')
        LoyaltyAccount.objects.create(user=user, lifetime_earned=200)
        self.assertEqual(services.get_dynamic_tier_for_user(user), self.bronze)

    def test_one_point_below_threshold_returns_the_lower_tier(self):
        user = CustomUser.objects.create_user(phone_number='09140077004')
        LoyaltyAccount.objects.create(user=user, lifetime_earned=199)
        self.assertEqual(services.get_dynamic_tier_for_user(user), self.base)

    def test_above_the_highest_threshold_returns_the_highest_tier(self):
        user = CustomUser.objects.create_user(phone_number='09140077005')
        LoyaltyAccount.objects.create(user=user, lifetime_earned=999999)
        self.assertEqual(services.get_dynamic_tier_for_user(user), self.gold)

    def test_inactive_tier_is_ignored(self):
        self.gold.is_active = False
        self.gold.save(update_fields=['is_active'])
        user = CustomUser.objects.create_user(phone_number='09140077006')
        LoyaltyAccount.objects.create(user=user, lifetime_earned=999999)
        self.assertEqual(services.get_dynamic_tier_for_user(user), self.silver)   # نه طلایی، چون غیرفعال است

    def test_reading_tier_immediately_after_credit_on_the_same_user_object_is_not_stale(self):
        """
        رگرسیون صریح روی یک تله‌ی واقعی جنگو که هنگام نوشتن این تست کشف شد: اگر
        get_dynamic_tier_for_user از توصیف‌گر رابطه‌ی معکوس user.loyalty_account (به‌جای کوئری
        مستقیم LoyaltyAccount.objects.get) استفاده می‌کرد، چون credit_points -> get_or_create_for_user
        یک LoyaltyAccount تازه (با lifetime_earned=0) می‌سازد و جنگو آن نمونه را خودکار روی
        همین آبجکت user کش می‌کند، در حالی که select_for_update بعدی مقدار را روی یک نمونه‌ی
        *جداگانه* بالا می‌برد و ذخیره می‌کند - خواندن از user.loyalty_account (که هنوز به همان
        نمونه‌ی قدیمیِ کش‌شده اشاره دارد) عدد صفر/قدیمی برمی‌گرداند، نه مقدار واقعی در دیتابیس.
        """
        user = CustomUser.objects.create_user(phone_number='09140077010')
        services.credit_points(user, 500, LoyaltyTransaction.EARN_ORDER, 'کسب اولیه')
        # همان user (همان آبجکت پایتون) که credit_points از آن استفاده کرد - نه یک fetch تازه
        self.assertEqual(services.get_dynamic_tier_for_user(user), self.silver)

    def test_spending_points_never_affects_the_computed_tier(self):
        """ lifetime_redeemed/current_balance هیچ نقشی در محاسبه‌ی سطح ندارند - فقط lifetime_earned. """
        user = CustomUser.objects.create_user(phone_number='09140077007')
        services.credit_points(user, 500, LoyaltyTransaction.EARN_ORDER, 'کسب اولیه')
        self.assertEqual(services.get_dynamic_tier_for_user(user), self.silver)

        services.debit_points(user, 400, LoyaltyTransaction.REDEEM_WALLET, 'خرج امتیاز')
        account = LoyaltyAccount.objects.get(user=user)
        self.assertEqual(account.current_balance, 100)      # موجودی قابل‌خرج کم شده
        self.assertEqual(account.lifetime_redeemed, 400)
        self.assertEqual(account.lifetime_earned, 500)       # دست‌نخورده

        self.assertEqual(services.get_dynamic_tier_for_user(user), self.silver)   # سطح همچنان نقره‌ای، نه پایین‌تر

    def test_function_is_pure_read_only(self):
        """ نه LoyaltyAccount نه LoyaltyTransaction ای اینجا ساخته می‌شود - نه برای کاربر بی‌حساب، نه برای کاربر با حساب. """
        no_account_user = CustomUser.objects.create_user(phone_number='09140077008')
        with_account_user = CustomUser.objects.create_user(phone_number='09140077009')
        LoyaltyAccount.objects.create(user=with_account_user, lifetime_earned=300)

        accounts_before = LoyaltyAccount.objects.count()
        transactions_before = LoyaltyTransaction.objects.count()

        services.get_dynamic_tier_for_user(no_account_user)
        services.get_dynamic_tier_for_user(with_account_user)
        services.get_dynamic_tier_for_user(no_account_user)   # چند بار فراخوانی - همچنان بدون Side-Effect

        self.assertEqual(LoyaltyAccount.objects.count(), accounts_before)
        self.assertEqual(LoyaltyTransaction.objects.count(), transactions_before)

    def test_get_tier_for_lifetime_points_regression(self):
        """ رگرسیون مستقیم روی تابع زیرین (فاز ۳A) که get_dynamic_tier_for_user به آن متکی است. """
        self.assertEqual(services.get_tier_for_lifetime_points(0), self.base)
        self.assertEqual(services.get_tier_for_lifetime_points(200), self.bronze)
        self.assertEqual(services.get_tier_for_lifetime_points(1199), self.silver)
        self.assertEqual(services.get_tier_for_lifetime_points(1200), self.gold)
