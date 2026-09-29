"""
تست‌های Loyalty Phase 5B-1: نگاشت صریح سطوح داینامیک به مقیاس صلب سنتی
(LoyaltyTier.legacy_equivalent_index).

عمداً هیچ اثر اجرایی‌ای تست نمی‌شود - این فیلد در این فاز صرفاً داده است؛
loyalty/stats.py::effective_loyalty_index هنوز آن را نمی‌خواند (موکول به ۵B-2). فقط ذخیره،
اعتبارسنجی clean() و سناریوهای کاردینالیتی مختلف اینجا سنجیده می‌شوند.
"""

from django.core.exceptions import ValidationError
from django.test import TestCase

from .models import LoyaltyTier


def _make_tier(title, rank, threshold, **overrides):
    data = dict(title=title, rank=rank, threshold=threshold)
    data.update(overrides)
    return LoyaltyTier.objects.create(**data)


class IsolatedTierTestCase(TestCase):
    """ هر تستی که از این ارث ببرد، با جدول کاملاً خالی از LoyaltyTier شروع می‌کند (Seed فاز ۳C را هم پاک می‌کند). """

    def setUp(self):
        super().setUp()
        LoyaltyTier.objects.all().delete()   # bulk QuerySet.delete() - از delete() نمونه‌ای/مسدودشده عبور نمی‌کند و مجاز است


class StoredValueTests(IsolatedTierTestCase):
    """ ذخیره و مقادیر مجاز (بازه‌ی ۰ تا ۴). """

    def test_values_within_range_are_stored_correctly(self):
        for value in (0, 1, 2, 3, 4):
            with self.subTest(value=value):
                LoyaltyTier.objects.all().delete()
                base = _make_tier('پایه', 0, 0, legacy_equivalent_index=0)
                tier = _make_tier('سطح تست', 1, 100, legacy_equivalent_index=value)
                tier.full_clean()   # نباید خطا بدهد
                self.assertEqual(LoyaltyTier.objects.get(pk=tier.pk).legacy_equivalent_index, value)

    def test_none_is_the_default_and_means_no_mapping_defined(self):
        base = _make_tier('پایه', 0, 0)
        self.assertIsNone(base.legacy_equivalent_index)
        base.full_clean()   # مقدار خالی همیشه مجاز است


class FieldValidatorTests(IsolatedTierTestCase):
    """ رد مقادیر خارج از بازه‌ی مجاز (اعتبارسنجی سطح فیلد - MinValueValidator/MaxValueValidator). """

    def test_value_above_four_is_rejected(self):
        _make_tier('پایه', 0, 0, legacy_equivalent_index=0)
        tier = _make_tier('سطح تست', 1, 100, legacy_equivalent_index=5)
        with self.assertRaises(ValidationError):
            tier.full_clean()

    def test_negative_value_is_rejected_at_the_database_field_level(self):
        # PositiveSmallIntegerField خودش منفی را در سطح دیتابیس/فرم رد می‌کند؛ اینجا از طریق
        # validators صریح (MinValueValidator(0)) با full_clean سنجیده می‌شود.
        _make_tier('پایه', 0, 0, legacy_equivalent_index=0)
        tier = LoyaltyTier(title='سطح تست', rank=1, threshold=100, legacy_equivalent_index=-1)
        with self.assertRaises(ValidationError):
            tier.full_clean()


class BaseTierValidationTests(IsolatedTierTestCase):
    """ رد نگاشت غیرصفر برای سطح پایه (کمترین آستانه). """

    def test_nonzero_mapping_on_the_base_tier_is_rejected(self):
        base = _make_tier('پایه', 0, 0, legacy_equivalent_index=1)
        with self.assertRaises(ValidationError):
            base.full_clean()

    def test_zero_mapping_on_the_base_tier_is_accepted(self):
        base = _make_tier('پایه', 0, 0, legacy_equivalent_index=0)
        base.full_clean()   # نباید خطا بدهد

    def test_none_mapping_on_the_base_tier_is_accepted(self):
        base = _make_tier('پایه', 0, 0)
        base.full_clean()   # نباید خطا بدهد

    def test_base_tier_check_applies_even_when_saving_a_different_tier(self):
        """ اعتبارسنجی روی کل مجموعه اجرا می‌شود؛ اگر سطح پایه (از قبل ذخیره‌شده) نگاشت غیرصفر
        داشته باشد، ذخیره‌ی هر سطح دیگری هم باید همان خطا را بگیرد. """
        LoyaltyTier.objects.create(title='پایه', rank=0, threshold=0, legacy_equivalent_index=None)
        base = LoyaltyTier.objects.get(title='پایه')
        LoyaltyTier.objects.filter(pk=base.pk).update(legacy_equivalent_index=1)   # دورزدن clean عمدی برای شبیه‌سازی داده‌ی قبلاً نامعتبر

        other = _make_tier('سطح دوم', 1, 100, legacy_equivalent_index=2)
        with self.assertRaises(ValidationError):
            other.full_clean()


class AscendingOrderValidationTests(IsolatedTierTestCase):
    """ رد نقض ترتیب صعودی (سطح با آستانه‌ی بالاتر، اندیس معادل پایین‌تر). """

    def setUp(self):
        super().setUp()
        _make_tier('پایه', 0, 0, legacy_equivalent_index=0)
        _make_tier('برنزی', 1, 100, legacy_equivalent_index=1)

    def test_descending_mapping_is_rejected(self):
        gold = _make_tier('طلایی', 2, 500, legacy_equivalent_index=0)   # کمتر از برنزی (۱) - نامعتبر
        with self.assertRaises(ValidationError):
            gold.full_clean()

    def test_equal_mapping_to_the_previous_tier_is_accepted(self):
        """ چند رتبه‌ی داینامیک می‌توانند به یک سطح سنتی نگاشت شوند (غیرنزولی، نه اکیداً صعودی). """
        gold = _make_tier('طلایی', 2, 500, legacy_equivalent_index=1)   # برابر با برنزی - مجاز
        gold.full_clean()

    def test_strictly_ascending_mapping_is_accepted(self):
        gold = _make_tier('طلایی', 2, 500, legacy_equivalent_index=2)
        gold.full_clean()

    def test_unmapped_middle_tier_does_not_break_the_ascending_check(self):
        """ سطح میانی بدون نگاشت (None) باید نادیده گرفته شود، نه این‌که زنجیره را قطع کند. """
        _make_tier('نقره‌ای', 2, 300)   # None - بدون نگاشت
        diamond = _make_tier('الماسی', 3, 1000, legacy_equivalent_index=0)   # کمتر از برنزی (۱) - باید همچنان رد شود
        with self.assertRaises(ValidationError):
            diamond.full_clean()


class CardinalityScenarioTests(IsolatedTierTestCase):
    """ سناریوهای کاردینالیتی مختلف (۳، ۵ و ۸ سطح) با نگاشت‌های معتبر. """

    def test_three_dynamic_tiers_with_valid_mapping(self):
        _make_tier('پایه', 0, 0, legacy_equivalent_index=0)
        _make_tier('میانی', 1, 500, legacy_equivalent_index=2)
        top = _make_tier('برتر', 2, 2000, legacy_equivalent_index=4)
        top.full_clean()
        self.assertEqual(LoyaltyTier.objects.count(), 3)

    def test_five_dynamic_tiers_mirroring_legacy_exactly(self):
        titles_and_thresholds = [('پایه', 0), ('برنزی', 200), ('نقره‌ای', 500), ('طلایی', 1200), ('الماسی', 2500)]
        last = None
        for rank, (title, threshold) in enumerate(titles_and_thresholds):
            last = _make_tier(title, rank, threshold, legacy_equivalent_index=rank)
        last.full_clean()
        self.assertEqual(LoyaltyTier.objects.count(), 5)

    def test_eight_dynamic_tiers_with_many_to_one_mapping(self):
        """ ۸ سطح داینامیک، چند رتبه به یک اندیس سنتی نگاشت می‌شوند (کاردینالیتی نامتقارن). """
        mapping = [0, 0, 1, 1, 2, 2, 3, 4]   # رتبه‌های ۰..۷ -> اندیس سنتی
        last = None
        for rank, legacy_index in enumerate(mapping):
            last = _make_tier(f'سطح {rank}', rank, rank * 100, legacy_equivalent_index=legacy_index)
        last.full_clean()
        self.assertEqual(LoyaltyTier.objects.count(), 8)

    def test_eight_dynamic_tiers_with_some_unmapped(self):
        """ برخی سطوح می‌توانند اصلاً نگاشت نداشته باشند (None)، حتی در کاردینالیتی بالا. """
        mappings = [0, None, 1, None, 2, None, 3, 4]
        last = None
        for rank, legacy_index in enumerate(mappings):
            last = _make_tier(f'سطح {rank}', rank, rank * 100, legacy_equivalent_index=legacy_index)
        last.full_clean()
        self.assertEqual(LoyaltyTier.objects.filter(legacy_equivalent_index__isnull=True).count(), 3)
