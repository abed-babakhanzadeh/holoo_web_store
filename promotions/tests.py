"""
تست‌های اپ تخفیف‌ها: سیاست سراسری، مدل و اعتبارسنجی، ماتریس اهداف/نوع/ترکیب/مخاطب، شاخص و کش، اتصال به قیمت‌گذاری،
باکس شگفت‌انگیز، ادمین و مهاجرت تخفیف‌های قدیمی.
"""

import itertools
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import jdatetime
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomUser
from products.deals import flash_deals_filter as registered_flash_filter
from products.models import Brand, Category, Product
from products.pricing import CASH, CHECK, VIP, final_price, price_breakdown

from . import index
from .flash import flash_deals_filter
from .models import DiscountPolicy, Promotion, PromotionTarget, parse_price_levels
from .testing import PromotionTestMixin, make_promotion, reset_promotions_cache

_seq = itertools.count(1)


class PromotionsTestBase(PromotionTestMixin, TestCase):
    """
    درخت دسته: root ← child ← grandchild (+ یک دسته‌ی مستقل)، دو برند و چند محصول با قیمت ۱۰۰٬۰۰۰ (نقدی ۹۰٬۰۰۰،
    ویژه ۸۰٬۰۰۰). کاربرها: سطح ۱ (چکی)، سطح ۲ (نقدی)، ویژه (سطح ۳).
    """

    def setUp(self):
        super().setUp()
        DiscountPolicy.load()
        self.root = Category.objects.create(name='ریشه', slug=f'promo-root-{next(_seq)}')
        self.child = Category.objects.create(name='فرزند', slug=f'promo-child-{next(_seq)}', parent=self.root)
        self.grandchild = Category.objects.create(name='نوه', slug=f'promo-grand-{next(_seq)}', parent=self.child)
        self.other_cat = Category.objects.create(name='مستقل', slug=f'promo-other-{next(_seq)}')
        self.brand_a = Brand.objects.create(name='برند الف', slug=f'promo-brand-a-{next(_seq)}')
        self.brand_b = Brand.objects.create(name='برند ب', slug=f'promo-brand-b-{next(_seq)}')

        self.p_root = self.product('در ریشه', self.root)
        self.p_child = self.product('در فرزند', self.child)
        self.p_grand = self.product('در نوه', self.grandchild)
        self.p_other = self.product('مستقل', self.other_cat)
        self.p_brand_a = self.product('برند الف', self.other_cat, brand=self.brand_a)

        self.level1 = CustomUser.objects.create_user(phone_number=f'0912001{next(_seq):04d}', price_level=1)
        self.level2 = CustomUser.objects.create_user(phone_number=f'0912001{next(_seq):04d}', price_level=2)
        self.vip = CustomUser.objects.create_user(phone_number=f'0912001{next(_seq):04d}', price_level=3)

    def product(self, name, category, brand=None, price=100000, **extra):
        n = next(_seq)
        data = dict(name=name, slug=f'promo-p-{n}', erp_code=f'ERP-PROMO-{n}', category=category, brand=brand,
                    price=price, price2=90000, price3=80000, stock=10)
        data.update(extra)
        return Product.objects.create(**data)

    def price(self, product, user=None, method=CHECK):
        return final_price(product, user or self.level1, method)

    def set_policy(self, **fields):
        policy = DiscountPolicy.load()
        for name, value in fields.items():
            setattr(policy, name, value)
        policy.save()


# ======================================================================== سیاست سراسری
class PolicyScopeGuardTests(TestCase):
    """
    apply_to_vip و بقیه‌ی فلگ‌های DiscountPolicy فقط مخصوص «تخفیف‌های خودکار» (Promotion)اند. این نگهبان جلوی این را
    می‌گیرد که کدی بیرون از موتور تخفیف خودکار آن‌ها را بخواند (مثلاً منطق کوپن در مرحله‌ی ۳ ناخواسته تحت‌الشعاع
    قرار بگیرد). کوپن قواعد مخاطبِ خودش را دارد.
    """
    POLICY_ATTRS = ('apply_to_vip', 'apply_for_cash', 'apply_for_check', 'promotion_stacking',
                    'max_item_discount_percent', 'rounding_step', 'promotions_enabled')
    ALLOWED = {'promotions/models.py', 'promotions/index.py', 'promotions/resolver.py', 'promotions/admin.py',
               'promotions/flash.py', 'promotions/tests.py', 'promotions/management/commands/refresh_promotions_cache.py'}

    def test_policy_flags_are_read_only_inside_the_promotions_engine(self):
        import pathlib
        import re
        from django.conf import settings
        root = pathlib.Path(settings.BASE_DIR)
        pattern = re.compile(r'\b(?:' + '|'.join(self.POLICY_ATTRS) + r')\b')
        offenders = []
        for path in root.rglob('*.py'):
            relative = path.relative_to(root).as_posix()
            if any(part in relative.split('/') for part in ('venv', '.venv', 'migrations', 'node_modules', 'FA')):
                continue
            if relative in self.ALLOWED or relative.startswith('promotions/'):
                continue
            if path.name.startswith('tests'):                    # تست‌ها برای ساختن سناریو سیاست را تنظیم می‌کنند
                continue
            text = path.read_text(encoding='utf-8', errors='ignore')
            for line_no, line in enumerate(text.splitlines(), 1):
                code = line.split('#', 1)[0]
                if pattern.search(code):
                    offenders.append(f'{relative}:{line_no}: {line.strip()[:90]}')
        self.assertEqual(offenders, [], 'فلگ‌های سیاست تخفیف خودکار فقط باید در اپ promotions خوانده شوند')

    def test_help_text_states_the_flag_is_for_automatic_promotions_only(self):
        field = DiscountPolicy._meta.get_field('apply_to_vip')
        self.assertIn('تخفیف‌های خودکار', field.verbose_name)
        self.assertIn('روی کدهای تخفیف اثری ندارد', field.help_text)


class DiscountPolicyModelTests(PromotionsTestBase):
    def test_singleton_and_defaults(self):
        DiscountPolicy.objects.all().delete()
        policy = DiscountPolicy.load()
        self.assertEqual(policy.pk, 1)
        self.assertTrue(policy.promotions_enabled)
        self.assertFalse(policy.apply_to_vip)                          # پیش‌فرض: روی قیمت سطح ویژه تخفیف نمی‌خورد
        self.assertTrue(policy.apply_for_cash and policy.apply_for_check)
        self.assertEqual((policy.promotion_stacking, policy.max_item_discount_percent, policy.rounding_step), ('best', 90, 1))
        second = DiscountPolicy(promotions_enabled=False)
        second.save()
        self.assertEqual(DiscountPolicy.objects.count(), 1)             # ردیف دوم ساخته نمی‌شود
        self.assertFalse(DiscountPolicy.load().promotions_enabled)

    def test_delete_is_a_no_op(self):
        DiscountPolicy.load().delete()
        self.assertEqual(DiscountPolicy.objects.count(), 1)


# ======================================================================== مدل و اعتبارسنجی
class PromotionValidationTests(PromotionsTestBase):
    def promo(self, **overrides):
        now = timezone.now()
        data = dict(title='تخفیف', kind='percent', value=10, starts_at=now, ends_at=now + timedelta(days=1))
        data.update(overrides)
        return Promotion(**data)

    def assertInvalid(self, promotion, field):
        with self.assertRaises(ValidationError) as ctx:
            promotion.full_clean()
        self.assertIn(field, ctx.exception.message_dict)

    def test_valid_promotion_passes(self):
        self.promo().full_clean()

    def test_end_must_be_after_start(self):
        now = timezone.now()
        self.assertInvalid(self.promo(starts_at=now, ends_at=now), 'ends_at')
        self.assertInvalid(self.promo(starts_at=now, ends_at=now - timedelta(hours=1)), 'ends_at')

    def test_percent_must_be_between_1_and_99(self):
        for bad in (0, 100, 150):
            with self.subTest(value=bad):
                self.assertInvalid(self.promo(value=bad), 'value')
        for good in (1, 99):
            self.promo(value=good).full_clean()

    def test_fixed_and_special_price_need_a_positive_amount(self):
        self.assertInvalid(self.promo(kind='fixed', value=0), 'value')
        self.assertInvalid(self.promo(kind='special_price', value=0), 'value')
        self.promo(kind='fixed', value=5000).full_clean()

    def test_amount_cap_only_for_percent(self):
        self.assertInvalid(self.promo(kind='fixed', value=5000, max_discount_amount=1000), 'max_discount_amount')
        self.promo(kind='percent', value=10, max_discount_amount=1000).full_clean()

    def test_price_levels_format(self):
        for bad in ('a', '1,,2', '11', '0', '1;2'):
            with self.subTest(levels=bad):
                self.assertInvalid(self.promo(price_levels=bad), 'price_levels')
        for good in ('1', '1,2', '3,4,10', ''):
            self.promo(price_levels=good).full_clean()
        self.assertEqual(parse_price_levels('1, 2,x,5'), frozenset({1, 2, 5}))

    def test_price_levels_spaces_are_stripped_on_save(self):
        promotion = self.promo(price_levels='1, 2')
        promotion.save()
        self.assertEqual(promotion.price_levels, '1,2')

    def test_status_values(self):
        now = timezone.now()
        self.assertEqual(self.promo(starts_at=now - timedelta(days=1)).status(), 'active')
        self.assertEqual(self.promo(starts_at=now + timedelta(days=1), ends_at=now + timedelta(days=2)).status(), 'scheduled')
        self.assertEqual(self.promo(starts_at=now - timedelta(days=2), ends_at=now - timedelta(days=1)).status(), 'expired')
        self.assertEqual(self.promo(starts_at=now - timedelta(days=1), is_active=False).status(), 'inactive')
        self.assertEqual(self.promo(starts_at=now - timedelta(days=1)).status_label, 'فعال')

    def test_is_public_and_value_display(self):
        self.assertTrue(self.promo().is_public)
        for override in ({'login_required': True}, {'min_loyalty_level': 1}, {'price_levels': '1'}, {'payment_method': 'cash'}):
            with self.subTest(override=override):
                self.assertFalse(self.promo(**override).is_public)
        self.assertEqual(self.promo(value=25).value_display, '25٪')
        self.assertEqual(self.promo(kind='fixed', value=15000).value_display, '15,000 تومان')


class PromotionTargetValidationTests(PromotionsTestBase):
    def setUp(self):
        super().setUp()
        self.promotion = make_promotion(self.p_root)

    def target(self, **fields):
        return PromotionTarget(promotion=self.promotion, **fields)

    def test_clean_requires_the_matching_object(self):
        with self.assertRaises(ValidationError):
            self.target(target_type='product').full_clean()
        with self.assertRaises(ValidationError):
            self.target(target_type='category', product=self.p_root).full_clean()
        with self.assertRaises(ValidationError):
            self.target(target_type='all', product=self.p_root).full_clean()
        with self.assertRaises(ValidationError):
            self.target(target_type='all', is_exclusion=True).full_clean()
        self.target(target_type='category', category=self.child).full_clean()
        self.target(target_type='brand', brand=self.brand_a, is_exclusion=True).full_clean()
        self.target(target_type='all').full_clean()

    def test_database_check_constraint_rejects_extra_wrong_links(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            PromotionTarget.objects.create(promotion=self.promotion, target_type='category', category=self.child,
                                           product=self.p_root)                                    # دسته + محصول
        with self.assertRaises(IntegrityError), transaction.atomic():
            PromotionTarget.objects.create(promotion=self.promotion, target_type='all', brand=self.brand_a)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PromotionTarget.objects.create(promotion=self.promotion, target_type='product', product=self.p_root,
                                           brand=self.brand_a)

    def test_a_target_without_its_object_is_a_model_level_error_not_a_database_one(self):
        """ الزامِ «هدفِ درست» در clean()/ادمین است؛ دیتابیس NULLِ گذرای حذف‌های cascade را می‌پذیرد (پایین‌تر) """
        with self.assertRaises(ValidationError):
            PromotionTarget(promotion=self.promotion, target_type='product').full_clean()

    def test_deleting_a_targeted_product_category_or_brand_succeeds_and_drops_only_its_target(self):
        """ باگ مرحله‌ی ۱: روی SQL Server جنگو ستون FK را موقتاً NULL می‌کرد و قید قدیمی حذف را رد می‌کرد """
        by_category = make_promotion(percent=10, targets=[{'target_type': 'category', 'category': self.other_cat}], title='دسته')
        by_brand = make_promotion(percent=10, targets=[{'target_type': 'brand', 'brand': self.brand_b}], title='برند')
        by_product = make_promotion(self.p_grand, title='محصول')
        mixed = make_promotion(percent=10, targets=[{'target_type': 'product', 'product': self.p_child},
                                                    {'target_type': 'brand', 'brand': self.brand_a}], title='ترکیبی')

        self.p_grand.delete()
        self.assertEqual(by_product.targets.count(), 0)
        self.assertTrue(Promotion.objects.filter(pk=by_product.pk).exists())                 # خودِ تخفیف می‌ماند

        self.brand_b.delete()
        self.assertEqual(by_brand.targets.count(), 0)

        self.p_child.delete()
        self.assertEqual([t.target_type for t in mixed.targets.all()], ['brand'])            # فقط هدفِ محصول رفت

        Category.objects.filter(pk=self.other_cat.pk).delete()                                # دسته‌ی دارای محصول (SET_NULL)
        self.assertEqual(by_category.targets.count(), 0)

    def test_promotion_left_without_targets_matches_nothing_and_does_not_break_pricing(self):
        make_promotion(self.p_grand, percent=30)
        keeper = self.p_other
        make_promotion(keeper, percent=10)
        self.p_grand.delete()
        self.assertEqual(self.price(keeper), Decimal('90000'))
        breakdown = price_breakdown(keeper, self.level1, CHECK)
        self.assertEqual(len(breakdown.applied), 1)

    def test_descriptions(self):
        self.assertIn('در ریشه', str(self.target(target_type='product', product=self.p_root)))
        self.assertIn('و زیردسته‌ها', str(self.target(target_type='category', category=self.child)))
        self.assertNotIn('و زیردسته‌ها', str(self.target(target_type='category', category=self.child, include_descendants=False)))
        self.assertTrue(str(self.target(target_type='brand', brand=self.brand_a, is_exclusion=True)).startswith('استثنا'))
        self.assertEqual(str(self.target(target_type='all')), 'کل فروشگاه')


# ======================================================================== اهداف
class TargetMatchingTests(PromotionsTestBase):
    def test_no_promotion_no_discount(self):
        self.assertEqual(self.price(self.p_root), Decimal('100000'))

    def test_product_target_only_affects_that_product(self):
        make_promotion(self.p_root, percent=20)
        self.assertEqual(self.price(self.p_root), Decimal('80000'))
        self.assertEqual(self.price(self.p_child), Decimal('100000'))

    def test_category_with_descendants(self):
        make_promotion(percent=20, targets=[{'target_type': 'category', 'category': self.root, 'include_descendants': True}])
        for product in (self.p_root, self.p_child, self.p_grand):
            with self.subTest(product=product.name):
                self.assertEqual(self.price(product), Decimal('80000'))
        self.assertEqual(self.price(self.p_other), Decimal('100000'))

    def test_category_without_descendants(self):
        make_promotion(percent=20, targets=[{'target_type': 'category', 'category': self.root, 'include_descendants': False}])
        self.assertEqual(self.price(self.p_root), Decimal('80000'))
        self.assertEqual(self.price(self.p_child), Decimal('100000'))
        self.assertEqual(self.price(self.p_grand), Decimal('100000'))

    def test_sub_category_target_does_not_reach_its_parent(self):
        make_promotion(percent=20, targets=[{'target_type': 'category', 'category': self.child}])
        self.assertEqual(self.price(self.p_root), Decimal('100000'))
        self.assertEqual(self.price(self.p_child), Decimal('80000'))
        self.assertEqual(self.price(self.p_grand), Decimal('80000'))

    def test_brand_target(self):
        make_promotion(percent=20, targets=[{'target_type': 'brand', 'brand': self.brand_a}])
        self.assertEqual(self.price(self.p_brand_a), Decimal('80000'))
        self.assertEqual(self.price(self.p_other), Decimal('100000'))                 # محصول بدون برند

    def test_whole_store_target(self):
        make_promotion(percent=20, targets=[{'target_type': 'all'}])
        for product in (self.p_root, self.p_grand, self.p_other, self.p_brand_a):
            self.assertEqual(self.price(product), Decimal('80000'))

    def test_exclusions_win_over_inclusions(self):
        make_promotion(percent=20, targets=[
            {'target_type': 'all'},
            {'target_type': 'category', 'category': self.child, 'is_exclusion': True},          # با زیردسته‌ها
            {'target_type': 'product', 'product': self.p_other, 'is_exclusion': True},
            {'target_type': 'brand', 'brand': self.brand_a, 'is_exclusion': True},
        ])
        self.assertEqual(self.price(self.p_root), Decimal('80000'))
        self.assertEqual(self.price(self.p_child), Decimal('100000'))
        self.assertEqual(self.price(self.p_grand), Decimal('100000'))
        self.assertEqual(self.price(self.p_other), Decimal('100000'))
        self.assertEqual(self.price(self.p_brand_a), Decimal('100000'))

    def test_exclusion_without_descendants_only_excludes_the_category_itself(self):
        make_promotion(percent=20, targets=[
            {'target_type': 'category', 'category': self.root},
            {'target_type': 'category', 'category': self.child, 'include_descendants': False, 'is_exclusion': True},
        ])
        self.assertEqual(self.price(self.p_child), Decimal('100000'))
        self.assertEqual(self.price(self.p_grand), Decimal('80000'))

    def test_multiple_include_targets_are_or(self):
        make_promotion(percent=20, targets=[
            {'target_type': 'product', 'product': self.p_other},
            {'target_type': 'brand', 'brand': self.brand_b},
            {'target_type': 'category', 'category': self.grandchild},
        ])
        self.assertEqual(self.price(self.p_other), Decimal('80000'))
        self.assertEqual(self.price(self.p_grand), Decimal('80000'))
        self.assertEqual(self.price(self.p_root), Decimal('100000'))

    def test_promotion_without_targets_matches_nothing(self):
        make_promotion(percent=20, targets=[])
        self.assertEqual(self.price(self.p_root), Decimal('100000'))

    def test_moving_a_category_in_the_tree_is_picked_up_after_save(self):
        make_promotion(percent=20, targets=[{'target_type': 'category', 'category': self.other_cat}])
        self.assertEqual(self.price(self.p_grand), Decimal('100000'))
        self.child.parent = self.other_cat                                              # درخت عوض شد ← نوه زیر «مستقل» رفت
        self.child.save()
        self.assertEqual(self.price(self.p_grand), Decimal('80000'))


# ======================================================================== نوع تخفیف، سقف و گرد کردن
class DiscountKindTests(PromotionsTestBase):
    def test_percent(self):
        make_promotion(self.p_root, percent=25)
        self.assertEqual(self.price(self.p_root), Decimal('75000'))

    def test_percent_applies_on_top_of_the_payment_method_price(self):
        make_promotion(self.p_root, percent=50)
        self.assertEqual(self.price(self.p_root, self.level2, CASH), Decimal('45000'))

    def test_percent_with_amount_cap(self):
        make_promotion(self.p_root, percent=50, max_discount_amount=10000)
        self.assertEqual(self.price(self.p_root), Decimal('90000'))

    def test_fixed_amount(self):
        make_promotion(self.p_root, kind='fixed', value=15000)
        self.assertEqual(self.price(self.p_root), Decimal('85000'))

    def test_fixed_amount_larger_than_price_is_limited_by_the_global_cap(self):
        make_promotion(self.p_root, kind='fixed', value=500000)
        self.assertEqual(self.price(self.p_root), Decimal('10000'))                     # سقف سراسری ۹۰٪

    def test_special_price_only_when_lower_than_current_price(self):
        make_promotion(self.p_root, kind='special_price', value=60000)
        make_promotion(self.p_child, kind='special_price', value=120000)
        self.assertEqual(self.price(self.p_root), Decimal('60000'))
        self.assertEqual(self.price(self.p_child), Decimal('100000'))                  # قیمت ویژه‌ی بالاتر از قیمت فعلی بی‌اثر است

    def test_global_percent_cap_from_policy(self):
        make_promotion(self.p_root, percent=80)
        self.set_policy(max_item_discount_percent=50)
        self.assertEqual(self.price(self.p_root), Decimal('50000'))

    def test_default_global_cap_is_90_percent(self):
        make_promotion(self.p_root, percent=99)
        self.assertEqual(self.price(self.p_root), Decimal('10000'))

    def test_rounding_steps(self):
        product = self.product('گرد', self.other_cat, price=33333)
        make_promotion(product, percent=33)                                             # 33333 − 10999.89 = 22333.11
        self.assertEqual(self.price(product), Decimal('22333'))
        for step, expected in ((100, '22300'), (1000, '22000')):
            with self.subTest(step=step):
                self.set_policy(rounding_step=step)
                self.assertEqual(self.price(product), Decimal(expected))

    def test_rounding_never_raises_the_price_above_the_base(self):
        product = self.product('گرد۲', self.other_cat, price=1010)
        make_promotion(product, percent=1)                                              # ۱۰٫۱ تخفیف؛ گرد به ۱۰۰۰ نباید از پایه بالاتر برود
        self.set_policy(rounding_step=1000)
        self.assertLessEqual(self.price(product), Decimal('1010'))

    def test_discount_that_rounds_to_nothing_is_dropped(self):
        product = self.product('ناچیز', self.other_cat, price=1000)
        make_promotion(product, percent=1)                                              # ۱۰ تومان؛ با گرد ۱۰۰۰ ← تخفیفی نمی‌ماند
        self.set_policy(rounding_step=1000)
        breakdown = price_breakdown(product, self.level1, CHECK)
        self.assertEqual(breakdown.final, breakdown.base)
        self.assertFalse(breakdown.has_discount)

    def test_zero_priced_product_is_untouched(self):
        product = self.product('صفر', self.other_cat, price=0, price2=0, price3=0)
        make_promotion(product, percent=50)
        self.assertEqual(self.price(product), Decimal('0'))


# ======================================================================== ترکیب
class StackingTests(PromotionsTestBase):
    def setUp(self):
        super().setUp()
        self.low = make_promotion(self.p_root, percent=10, priority=2, title='ده درصد')
        self.high = make_promotion(self.p_root, percent=20, priority=1, title='بیست درصد')

    def test_best_mode_picks_the_biggest_saving_for_the_buyer(self):
        self.assertEqual(self.price(self.p_root), Decimal('80000'))
        breakdown = price_breakdown(self.p_root, self.level1, CHECK)
        self.assertEqual([a.title for a in breakdown.applied], ['بیست درصد'])

    def test_best_mode_tie_goes_to_priority_then_lowest_id(self):
        first = make_promotion(self.p_child, percent=10, priority=1, title='اول')
        second = make_promotion(self.p_child, percent=10, priority=5, title='دوم (اولویت بالاتر)')
        self.assertEqual(price_breakdown(self.p_child, self.level1, CHECK).promotion.title, 'دوم (اولویت بالاتر)')
        second.priority = 1
        second.save()
        self.assertEqual(price_breakdown(self.p_child, self.level1, CHECK).promotion.promotion_id, first.pk)

    def test_stack_mode_applies_in_priority_order_on_the_running_price(self):
        self.set_policy(promotion_stacking='stack')
        # اولویت بالاتر اول: ۱۰٪ ← ۹۰٬۰۰۰؛ بعد ۲۰٪ از ۹۰٬۰۰۰ = ۱۸٬۰۰۰ ← ۷۲٬۰۰۰
        self.assertEqual(self.price(self.p_root), Decimal('72000'))
        breakdown = price_breakdown(self.p_root, self.level1, CHECK)
        self.assertEqual([a.title for a in breakdown.applied], ['ده درصد', 'بیست درصد'])
        self.assertEqual(sum(a.discount for a in breakdown.applied), breakdown.discount_amount)

    def test_stack_mode_is_limited_by_the_global_cap(self):
        make_promotion(self.p_child, percent=50, priority=2)
        make_promotion(self.p_child, percent=50, priority=1)
        self.set_policy(promotion_stacking='stack', max_item_discount_percent=60)
        self.assertEqual(self.price(self.p_child), Decimal('40000'))                    # حداکثر ۶۰٪ روی قیمت پایه

    def test_stack_mode_with_a_single_promotion_equals_best(self):
        self.set_policy(promotion_stacking='stack')
        self.assertEqual(self.price(self.p_child), Decimal('100000'))
        make_promotion(self.p_child, percent=10)
        self.assertEqual(self.price(self.p_child), Decimal('90000'))


# ======================================================================== مخاطب، شرایط، زمان و سیاست
class AudienceAndConditionsTests(PromotionsTestBase):
    def test_inactive_expired_and_scheduled_promotions_are_ignored(self):
        make_promotion(self.p_root, percent=20, active=False)
        make_promotion(self.p_root, percent=20, expired=True)
        make_promotion(self.p_root, percent=20, scheduled=True)
        self.assertEqual(self.price(self.p_root), Decimal('100000'))

    def test_window_boundaries_are_evaluated_at_the_given_time(self):
        promotion = make_promotion(self.p_root, percent=20)
        inside, before, after = (promotion.starts_at + timedelta(hours=1), promotion.starts_at - timedelta(seconds=1),
                                 promotion.ends_at + timedelta(seconds=1))
        for now, expected in ((inside, '80000'), (before, '100000'), (after, '100000'),
                              (promotion.starts_at, '80000'), (promotion.ends_at, '80000')):
            with self.subTest(now=now):
                self.assertEqual(price_breakdown(self.p_root, self.level1, CHECK, now=now).final, Decimal(expected))

    def test_guest_gets_the_public_discount_with_level_one_price(self):
        make_promotion(self.p_root, percent=20)
        self.assertEqual(final_price(self.p_root, AnonymousUser(), CHECK), Decimal('80000'))
        self.assertEqual(final_price(self.p_root, None), Decimal('80000'))

    def test_login_required(self):
        make_promotion(self.p_root, percent=20, login_required=True)
        self.assertEqual(final_price(self.p_root, AnonymousUser()), Decimal('100000'))
        self.assertEqual(self.price(self.p_root), Decimal('80000'))

    def test_price_level_targeting(self):
        make_promotion(self.p_root, percent=20, price_levels='2')
        self.assertEqual(self.price(self.p_root, self.level1, CHECK), Decimal('100000'))
        self.assertEqual(self.price(self.p_root, self.level2, CASH), Decimal('72000'))          # ۹۰٬۰۰۰ منهای ۲۰٪

    def test_guest_counts_as_price_level_one(self):
        make_promotion(self.p_root, percent=20, price_levels='1')
        self.assertEqual(final_price(self.p_root, AnonymousUser()), Decimal('80000'))

    def test_payment_method_condition(self):
        make_promotion(self.p_root, percent=20, payment_method='cash')
        self.assertEqual(self.price(self.p_root, self.level1, CHECK), Decimal('100000'))
        self.assertEqual(self.price(self.p_root, self.level1, CASH), Decimal('72000'))

    def test_loyalty_level_targeting(self):
        make_promotion(self.p_root, percent=20, min_loyalty_level=2)                # «نقره‌ای» (≥ ۷ سفارش) و بالاتر
        cases = ((0, '100000'), (3, '100000'), (6, '100000'), (7, '80000'), (30, '80000'))
        for orders, expected in cases:
            with self.subTest(paid_orders=orders):
                user = CustomUser.objects.create_user(phone_number=f'0912002{next(_seq):04d}', price_level=1)
                user.paid_orders_count = orders                                     # cached_property؛ بدون کوئری سفارش‌ها
                self.assertEqual(final_price(self.p_root, user, CHECK), Decimal(expected))
        self.assertEqual(final_price(self.p_root, AnonymousUser(), CHECK), Decimal('100000'))

    def test_vip_users_get_no_automatic_discount_by_default(self):
        make_promotion(self.p_root, percent=20)
        self.assertEqual(self.price(self.p_root, self.vip, VIP), Decimal('80000'))              # قیمت ویژه‌ی خودش، بدون تخفیف
        self.assertEqual(price_breakdown(self.p_root, self.vip, VIP).final, Decimal('80000'))

    def test_vip_policy_flag_turns_the_discount_on(self):
        make_promotion(self.p_root, percent=20)
        self.set_policy(apply_to_vip=True)
        self.assertEqual(self.price(self.p_root, self.vip, VIP), Decimal('64000'))              # ۸۰٬۰۰۰ منهای ۲۰٪

    def test_vip_price_level_above_three_is_also_excluded(self):
        make_promotion(self.p_root, percent=20)
        user = CustomUser.objects.create_user(phone_number=f'0912003{next(_seq):04d}', price_level=6)
        self.assertEqual(price_breakdown(self.p_root, user).final, price_breakdown(self.p_root, user).base)

    def test_vip_user_with_a_non_vip_method_argument_is_still_treated_as_vip(self):
        make_promotion(self.p_root, percent=20)
        breakdown = price_breakdown(self.p_root, self.vip, CHECK)
        self.assertFalse(breakdown.has_discount)

    def test_payment_method_policy_flags(self):
        make_promotion(self.p_root, percent=20)
        self.set_policy(apply_for_cash=False)
        self.assertEqual(self.price(self.p_root, self.level2, CASH), Decimal('90000'))
        self.assertEqual(self.price(self.p_root, self.level1, CHECK), Decimal('80000'))
        self.set_policy(apply_for_cash=True, apply_for_check=False)
        self.assertEqual(self.price(self.p_root, self.level1, CHECK), Decimal('100000'))
        self.assertEqual(self.price(self.p_root, self.level2, CASH), Decimal('72000'))

    def test_promotions_can_be_switched_off_globally(self):
        make_promotion(self.p_root, percent=20)
        self.set_policy(promotions_enabled=False)
        self.assertEqual(self.price(self.p_root), Decimal('100000'))
        self.set_policy(promotions_enabled=True)
        self.assertEqual(self.price(self.p_root), Decimal('80000'))


# ======================================================================== شاخص و کش
class PromotionIndexTests(PromotionsTestBase):
    def test_build_uses_a_constant_number_of_queries(self):
        make_promotion(self.p_root)
        make_promotion(percent=10, targets=[{'target_type': 'category', 'category': self.root},
                                            {'target_type': 'brand', 'brand': self.brand_a}])
        with self.assertNumQueries(4):                                # سیاست + تخفیف‌ها + اهداف + درخت دسته
            built = index.build_index()
        self.assertEqual(len(built.rules), 2)

    def test_build_without_promotions_skips_targets_and_tree(self):
        with self.assertNumQueries(2):                                # سیاست + تخفیف‌ها
            self.assertEqual(index.build_index().rules, ())

    def test_pricing_many_products_needs_no_queries_once_the_index_is_warm(self):
        make_promotion(percent=20, targets=[{'target_type': 'category', 'category': self.root}])
        index.get_index()
        products = [self.p_root, self.p_child, self.p_grand, self.p_other, self.p_brand_a] * 20
        with self.assertNumQueries(0):
            for product in products:
                final_price(product, self.level1, CHECK)

    def test_second_read_comes_from_memory_without_queries(self):
        make_promotion(self.p_root)
        index.get_index()
        with self.assertNumQueries(0):
            index.get_index()

    def test_redis_serves_a_process_that_has_no_memo(self):
        make_promotion(self.p_root)
        index.get_index()
        index._memo['index'] = None                                   # پروسه‌ی دیگری که حافظه ندارد
        with self.assertNumQueries(0):
            self.assertEqual(len(index.get_index().rules), 1)

    def test_cache_key_contains_the_database_name(self):
        self.assertIn(connection.settings_dict['NAME'], index.cache_key())

    def test_every_relevant_change_invalidates_the_cache(self):
        promotion = make_promotion(self.p_root, percent=10)
        self.assertEqual(self.price(self.p_root), Decimal('90000'))

        promotion.value = 30                                          # ویرایش تخفیف
        promotion.save()
        self.assertEqual(self.price(self.p_root), Decimal('70000'))

        PromotionTarget.objects.create(promotion=promotion, target_type='product', product=self.p_child)   # هدف تازه
        self.assertEqual(self.price(self.p_child), Decimal('70000'))

        PromotionTarget.objects.filter(promotion=promotion, product=self.p_child).first().delete()          # حذف هدف
        self.assertEqual(self.price(self.p_child), Decimal('100000'))

        self.set_policy(max_item_discount_percent=20)                 # سیاست
        self.assertEqual(self.price(self.p_root), Decimal('80000'))

        promotion.delete()                                            # حذف تخفیف
        self.assertEqual(self.price(self.p_root), Decimal('100000'))

    def test_redis_outage_falls_back_to_the_database(self):
        make_promotion(self.p_root, percent=20)
        index.invalidate()
        with mock.patch.object(index.cache, 'get', side_effect=ConnectionError('redis down')), \
                mock.patch.object(index.cache, 'set', side_effect=ConnectionError('redis down')):
            self.assertEqual(self.price(self.p_root), Decimal('80000'))

    def test_invalidate_survives_a_redis_outage(self):
        with mock.patch.object(index.cache, 'delete', side_effect=ConnectionError('redis down')):
            index.invalidate()                                        # نباید خطا بدهد

    def test_expired_promotion_is_not_loaded_into_a_new_index(self):
        make_promotion(self.p_root, expired=True)
        self.assertEqual(index.build_index().rules, ())

    def test_scheduled_promotion_is_loaded_and_becomes_effective_with_time(self):
        promotion = make_promotion(self.p_root, percent=20, scheduled=True)
        self.assertEqual(len(index.get_index().rules), 1)
        self.assertEqual(price_breakdown(self.p_root, self.level1, CHECK).final, Decimal('100000'))
        later = promotion.starts_at + timedelta(minutes=1)            # بدون هیچ باطل‌سازی، فقط گذر زمان
        self.assertEqual(price_breakdown(self.p_root, self.level1, CHECK, now=later).final, Decimal('80000'))

    def test_rules_are_picklable_for_redis(self):
        import pickle
        make_promotion(percent=10, targets=[{'target_type': 'category', 'category': self.root}])
        built = index.build_index()
        self.assertEqual(pickle.loads(pickle.dumps(built)), built)


# ======================================================================== اتصال به قیمت‌گذاری و نمایش
class PricingIntegrationTests(PromotionsTestBase):
    def test_price_breakdown_fields(self):
        promotion = make_promotion(self.p_root, percent=25, badge_label='ویژه‌ی آخر هفته')
        breakdown = price_breakdown(self.p_root, self.level1, CHECK)
        self.assertEqual((breakdown.base, breakdown.final, breakdown.discount_amount), (Decimal('100000'), Decimal('75000'), Decimal('25000')))
        self.assertTrue(breakdown.has_discount)
        self.assertEqual(breakdown.percent, 25)
        self.assertEqual(breakdown.badge_label, 'ویژه‌ی آخر هفته')
        self.assertEqual(breakdown.ends_at, promotion.ends_at)
        self.assertEqual(breakdown.promotion.promotion_id, promotion.pk)

    def test_percent_equivalent_for_fixed_amount_discounts(self):
        make_promotion(self.p_root, kind='fixed', value=15000)
        self.assertEqual(price_breakdown(self.p_root, self.level1, CHECK).percent, 15)

    def test_breakdown_without_discount(self):
        breakdown = price_breakdown(self.p_root, self.level1, CHECK)
        self.assertEqual((breakdown.final, breakdown.percent, breakdown.badge_label, breakdown.ends_at, breakdown.promotion),
                         (Decimal('100000'), 0, '', None, None))

    def test_discount_none_bypasses_promotions(self):
        make_promotion(self.p_root, percent=25)
        self.assertEqual(final_price(self.p_root, self.level1, CHECK, discount=None), Decimal('100000'))

    def test_legacy_discount_objects_are_rejected(self):
        with self.assertRaises(TypeError):
            final_price(self.p_root, self.level1, CHECK, discount=object())

    def test_card_cart_and_invoice_agree(self):
        from cart.models import Cart, CartItem
        make_promotion(self.p_root, percent=20)
        card = self.p_root.get_discounted_price(self.level1)
        cart = Cart.objects.create(user=self.level1)
        item = CartItem.objects.create(cart=cart, product=self.p_root, quantity=3)
        self.assertEqual(card, Decimal('80000'))
        self.assertEqual(item.get_cost(), card * 3)

    def card_request(self):
        from django.test import RequestFactory
        request = RequestFactory().get('/')
        request.user = self.level1
        request.session = {}                                          # تگ compare به session نیاز دارد
        return request

    def test_price_info_template_tag(self):
        from django.template import Context, Template
        make_promotion(self.p_root, percent=20)
        template = Template('{% load product_tags %}{% price_info product user as price %}'
                            '{{ price.percent }}|{{ price.base|floatformat:0 }}|{{ price.final|floatformat:0 }}|{{ price.has_discount }}')
        html = template.render(Context({'product': self.p_root, 'user': self.level1}))
        self.assertEqual(html, '20|100000|80000|True')

    def test_card_and_detail_show_the_discount_and_the_strikethrough_price(self):
        make_promotion(self.p_root, percent=20, badge_label='ویژه')
        self.client.force_login(self.level1)
        html = self.client.get(reverse('products:product_detail', args=[self.p_root.slug])).content.decode()
        self.assertIn('٪20', html)
        self.assertIn('<del', html)
        self.assertIn('80000', html)
        self.assertIn('ویژه', html)

        from django.template.loader import render_to_string
        request = self.card_request()
        card = render_to_string('products/partials/product_card.html', {'product': self.p_root, 'request': request})
        self.assertIn('٪20', card)
        self.assertIn('<del', card)

    def test_card_without_discount_has_no_percent_badge(self):
        from django.template.loader import render_to_string
        request = self.card_request()
        card = render_to_string('products/partials/product_card.html', {'product': self.p_root, 'request': request})
        self.assertNotIn('<del', card)

    def test_vip_user_sees_no_discount_badge_by_default(self):
        make_promotion(self.p_root, percent=20)
        self.client.force_login(self.vip)
        html = self.client.get(reverse('products:product_detail', args=[self.p_root.slug])).content.decode()
        self.assertNotIn('٪20', html)

    def test_out_of_stock_card_keeps_the_taller_footer_only_when_discounted(self):
        from django.template.loader import render_to_string
        request = self.card_request()
        sold_out = self.product('ناموجود', self.other_cat, stock=0)
        plain = render_to_string('products/partials/product_card.html', {'product': sold_out, 'request': request})
        make_promotion(sold_out, percent=20)
        discounted = render_to_string('products/partials/product_card.html', {'product': sold_out, 'request': request})
        self.assertIn('min-height:61px', plain)
        self.assertIn('min-height:97px', discounted)


# ======================================================================== باکس شگفت‌انگیز
class FlashDealsTests(PromotionsTestBase):
    def deals(self, category_ids=None):
        from products.views import _flash_deals
        products, ends_at = _flash_deals(category_ids)
        return list(products), ends_at

    def test_registered_provider_is_the_promotions_one(self):
        self.assertEqual(registered_flash_filter.__module__, 'products.deals')
        make_promotion(self.p_root)
        q, ends_at = registered_flash_filter()
        self.assertIsNotNone(q)
        self.assertIsNotNone(ends_at)

    def test_no_promotion_no_deals(self):
        self.assertEqual(self.deals(), ([], None))

    def test_product_category_brand_and_all_targets_feed_the_box(self):
        make_promotion(self.p_root)
        products, ends_at = self.deals()
        self.assertEqual(products, [self.p_root])

        make_promotion(percent=10, targets=[{'target_type': 'category', 'category': self.child}])
        self.assertEqual({p.pk for p in self.deals()[0]}, {self.p_root.pk, self.p_child.pk, self.p_grand.pk})

        make_promotion(percent=10, targets=[{'target_type': 'brand', 'brand': self.brand_a}])
        self.assertIn(self.p_brand_a, self.deals()[0])

    def test_all_store_promotion_lists_the_catalogue_capped_at_ten(self):
        for i in range(12):
            self.product(f'اضافه {i}', self.other_cat)
        make_promotion(percent=10, targets=[{'target_type': 'all'}])
        self.assertEqual(len(self.deals()[0]), 10)

    def test_exclusions_remove_products_from_the_box(self):
        make_promotion(percent=10, targets=[{'target_type': 'category', 'category': self.root},
                                            {'target_type': 'product', 'product': self.p_child, 'is_exclusion': True}])
        listed = {p.pk for p in self.deals()[0]}
        self.assertEqual(listed, {self.p_root.pk, self.p_grand.pk})

    def test_non_public_and_hidden_promotions_are_not_advertised(self):
        make_promotion(self.p_root, login_required=True)
        make_promotion(self.p_child, min_loyalty_level=1)
        make_promotion(self.p_grand, price_levels='1')
        make_promotion(self.p_other, payment_method='cash')
        make_promotion(self.p_brand_a, show_in_flash_deals=False)
        self.assertEqual(self.deals(), ([], None))

    def test_inactive_expired_scheduled_are_not_advertised_and_policy_switch_hides_all(self):
        make_promotion(self.p_root, active=False)
        make_promotion(self.p_child, expired=True)
        make_promotion(self.p_grand, scheduled=True)
        self.assertEqual(self.deals(), ([], None))
        make_promotion(self.p_other)
        self.assertEqual(len(self.deals()[0]), 1)
        self.set_policy(promotions_enabled=False)
        self.assertEqual(self.deals(), ([], None))

    def test_category_page_restricts_products_and_timer_to_that_category(self):
        soon = make_promotion(self.p_other, title='زودتر', ends_at=timezone.now() + timedelta(hours=1))
        later = make_promotion(self.p_child, title='دیرتر', ends_at=timezone.now() + timedelta(days=5))
        products, ends_at = self.deals()
        self.assertEqual({p.pk for p in products}, {self.p_other.pk, self.p_child.pk})
        self.assertEqual(ends_at, soon.ends_at)

        products, ends_at = self.deals(category_ids=[self.root.pk, self.child.pk, self.grandchild.pk])
        self.assertEqual([p.pk for p in products], [self.p_child.pk])
        self.assertEqual(ends_at, later.ends_at)                        # تایمر فقط از تخفیف‌های همان دسته

    def test_timer_is_the_nearest_end_among_advertised_promotions(self):
        make_promotion(self.p_root, ends_at=timezone.now() + timedelta(days=3))
        nearest = make_promotion(self.p_child, ends_at=timezone.now() + timedelta(hours=5))
        make_promotion(self.p_grand, ends_at=timezone.now() + timedelta(days=9), login_required=True)   # تبلیغ نمی‌شود
        self.assertEqual(self.deals()[1], nearest.ends_at)

    def test_promotion_without_visible_products_does_not_set_the_timer(self):
        hidden = self.product('پنهان', self.other_cat, is_active=False)
        make_promotion(hidden, ends_at=timezone.now() + timedelta(hours=1))
        visible_promo = make_promotion(self.p_root, ends_at=timezone.now() + timedelta(days=2))
        self.assertEqual(self.deals()[1], visible_promo.ends_at)

    def test_home_page_renders_the_amazing_box(self):
        make_promotion(self.p_root, percent=30)
        response = self.client.get(reverse('products:home'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([p.pk for p in response.context['flash_deal_products']], [self.p_root.pk])
        self.assertIsNotNone(response.context['deal_ends_at'])
        self.assertContains(response, self.p_root.name)

    def test_flash_filter_helper_directly(self):
        make_promotion(self.p_root)
        q, _ = flash_deals_filter()
        self.assertEqual(list(Product.objects.filter(q)), [self.p_root])


# ======================================================================== ادمین
def _jalali_fields(prefix, dt):
    jd = jdatetime.datetime.fromgregorian(datetime=timezone.localtime(dt))
    return {f'{prefix}_0': jd.strftime('%Y/%m/%d'), f'{prefix}_1': jd.strftime('%H:%M')}


class PromotionAdminTests(PromotionsTestBase):
    def setUp(self):
        super().setUp()
        self.admin_user = CustomUser.objects.create_superuser(phone_number=f'0912004{next(_seq):04d}')
        self.client.force_login(self.admin_user)

    def form_data(self, **overrides):
        now = timezone.now()
        data = {
            'title': 'تخفیف ادمین', 'kind': 'percent', 'value': '15', 'max_discount_amount': '',
            'is_active': 'on', 'priority': '0', 'min_loyalty_level': '0', 'price_levels': '', 'payment_method': '',
            'show_in_flash_deals': 'on', 'badge_label': '',
            'targets-TOTAL_FORMS': '1', 'targets-INITIAL_FORMS': '0', 'targets-MIN_NUM_FORMS': '0', 'targets-MAX_NUM_FORMS': '1000',
            'targets-0-target_type': 'product', 'targets-0-product': str(self.p_root.pk), 'targets-0-category': '',
            'targets-0-brand': '', 'targets-0-include_descendants': 'on',
        }
        data.update(_jalali_fields('starts_at', now - timedelta(days=1)))
        data.update(_jalali_fields('ends_at', now + timedelta(days=3)))
        data.update(overrides)
        return {k: v for k, v in data.items() if v is not None}

    # ----- سیاست -----
    def test_policy_changelist_redirects_to_the_single_form_and_saves(self):
        response = self.client.get(reverse('admin:promotions_discountpolicy_changelist'))
        self.assertRedirects(response, reverse('admin:promotions_discountpolicy_change', args=[1]), fetch_redirect_response=False)
        page = self.client.get(reverse('admin:promotions_discountpolicy_change', args=[1]))
        self.assertEqual(page.status_code, 200)
        for name in ('promotions_enabled', 'apply_to_vip', 'apply_for_cash', 'apply_for_check', 'promotion_stacking',
                     'max_item_discount_percent', 'rounding_step'):
            self.assertContains(page, f'name="{name}"')

        make_promotion(self.p_root, percent=20)
        self.assertEqual(self.price(self.p_root, self.vip, VIP), Decimal('80000'))
        response = self.client.post(reverse('admin:promotions_discountpolicy_change', args=[1]), {
            'promotions_enabled': 'on', 'apply_to_vip': 'on', 'apply_for_cash': 'on', 'apply_for_check': 'on',
            'promotion_stacking': 'best', 'max_item_discount_percent': '90', 'rounding_step': '1',
            'coupon_reservation_minutes': '30', 'coupon_max_invalid_attempts': '10', 'coupon_attempt_window_minutes': '60',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.price(self.p_root, self.vip, VIP), Decimal('64000'))              # اثر فوری

    def test_policy_cannot_be_added_twice_or_deleted(self):
        self.assertFalse(self.client.get(reverse('admin:promotions_discountpolicy_add')).status_code == 200)

    # ----- تخفیف -----
    def test_create_promotion_with_a_target_via_admin(self):
        response = self.client.post(reverse('admin:promotions_promotion_add'), self.form_data())
        self.assertEqual(response.status_code, 302, getattr(response, 'context', None) and response.context['adminform'].form.errors)
        promotion = Promotion.objects.get(title='تخفیف ادمین')
        self.assertEqual(promotion.targets.count(), 1)
        self.assertEqual(self.price(self.p_root), Decimal('85000'))

    def test_promotion_needs_at_least_one_include_target(self):
        data = self.form_data(**{'targets-TOTAL_FORMS': '0', 'targets-0-target_type': None, 'targets-0-product': None,
                                 'targets-0-category': None, 'targets-0-brand': None, 'targets-0-include_descendants': None})
        response = self.client.post(reverse('admin:promotions_promotion_add'), data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'حداقل یک هدفِ')
        self.assertFalse(Promotion.objects.filter(title='تخفیف ادمین').exists())

    def test_only_exclusion_targets_are_not_enough(self):
        data = self.form_data(**{'targets-0-is_exclusion': 'on'})
        response = self.client.post(reverse('admin:promotions_promotion_add'), data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'حداقل یک هدفِ')

    def test_model_validation_errors_are_shown(self):
        response = self.client.post(reverse('admin:promotions_promotion_add'), self.form_data(value='150'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'بین ۱ تا ۹۹')

    def test_list_shows_target_status_and_schedule_and_filters(self):
        active = make_promotion(self.p_root, title='فعال‌ترین')
        expired = make_promotion(self.p_child, title='منقضی‌ترین', expired=True)
        scheduled = make_promotion(self.p_grand, title='آینده‌ترین', scheduled=True)
        inactive = make_promotion(self.p_other, title='غیرفعال‌ترین', active=False)
        url = reverse('admin:promotions_promotion_changelist')
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'محصول «در ریشه»')
        self.assertContains(page, 'فعال')
        for status, wanted, unwanted in (('active', active, [expired, scheduled, inactive]),
                                         ('expired', expired, [active, scheduled, inactive]),
                                         ('scheduled', scheduled, [active, expired, inactive]),
                                         ('inactive', inactive, [active, expired, scheduled])):
            with self.subTest(status=status):
                body = self.client.get(url, {'status': status}).content.decode()
                self.assertIn(f'/{wanted.pk}/change/', body)
                for other in unwanted:
                    self.assertNotIn(f'/{other.pk}/change/', body)

    def test_search_by_target_names(self):
        target = make_promotion(percent=10, targets=[{'target_type': 'category', 'category': self.other_cat}], title='دسته‌ای')
        other = make_promotion(self.p_root, title='محصولی')
        body = self.client.get(reverse('admin:promotions_promotion_changelist'), {'q': 'مستقل'}).content.decode()
        self.assertIn(f'/{target.pk}/change/', body)
        self.assertNotIn(f'/{other.pk}/change/', body)

    def test_change_page_shows_preview_of_affected_products(self):
        promotion = make_promotion(percent=10, targets=[{'target_type': 'category', 'category': self.root}])
        page = self.client.get(reverse('admin:promotions_promotion_change', args=[promotion.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, '3</b> محصول مشمول است')

    def test_preview_warns_when_nothing_matches(self):
        promotion = make_promotion(percent=10, targets=[])
        page = self.client.get(reverse('admin:promotions_promotion_change', args=[promotion.pk]))
        self.assertContains(page, 'هیچ محصول قابل‌نمایشی مشمول نیست')

    def test_overlap_warning_after_saving(self):
        make_promotion(percent=10, targets=[{'target_type': 'category', 'category': self.root}], title='همپوشان قبلی')
        response = self.client.post(reverse('admin:promotions_promotion_add'), self.form_data(), follow=True)
        self.assertContains(response, 'هم‌پوشانی')
        self.assertContains(response, 'همپوشان قبلی')

    def test_no_overlap_warning_when_products_differ(self):
        make_promotion(self.p_other, title='جدا')
        response = self.client.post(reverse('admin:promotions_promotion_add'), self.form_data(), follow=True)
        self.assertNotContains(response, 'هم‌پوشانی')

    def action(self, name, *promotions):
        return self.client.post(reverse('admin:promotions_promotion_changelist'), {
            'action': name, '_selected_action': [p.pk for p in promotions],
        }, follow=True)

    def test_activate_and_deactivate_actions_refresh_prices_immediately(self):
        promotion = make_promotion(self.p_root, percent=20, active=False)
        self.assertEqual(self.price(self.p_root), Decimal('100000'))
        self.action('activate', promotion)
        self.assertEqual(self.price(self.p_root), Decimal('80000'))
        self.action('deactivate', promotion)
        self.assertEqual(self.price(self.p_root), Decimal('100000'))

    def test_extend_actions(self):
        promotion = make_promotion(self.p_root)
        old_end = promotion.ends_at
        self.action('extend_7_days', promotion)
        promotion.refresh_from_db()
        self.assertEqual(promotion.ends_at, old_end + timedelta(days=7))
        self.action('extend_30_days', promotion)
        promotion.refresh_from_db()
        self.assertEqual(promotion.ends_at, old_end + timedelta(days=37))

    def test_expired_promotion_can_be_revived_by_extension(self):
        promotion = make_promotion(self.p_root, percent=20, expired=True)
        self.assertEqual(self.price(self.p_root), Decimal('100000'))
        self.action('extend_7_days', promotion)
        self.assertEqual(self.price(self.p_root), Decimal('80000'))

    def test_duplicate_action_creates_inactive_copy_with_same_targets(self):
        promotion = make_promotion(percent=10, targets=[{'target_type': 'category', 'category': self.root},
                                                        {'target_type': 'product', 'product': self.p_child, 'is_exclusion': True}])
        self.action('duplicate', promotion)
        copy = Promotion.objects.exclude(pk=promotion.pk).get()
        self.assertTrue(copy.title.endswith('(کپی)'))
        self.assertFalse(copy.is_active)
        self.assertEqual(copy.targets.count(), 2)
        self.assertEqual(copy.targets.filter(is_exclusion=True).count(), 1)
        self.assertEqual(self.price(self.p_root), Decimal('90000'))                              # فقط اصلی اثر دارد

    def test_status_column_uses_persian_labels(self):
        make_promotion(self.p_root)
        self.assertContains(self.client.get(reverse('admin:promotions_promotion_changelist')), 'فعال')

    def test_products_admin_no_longer_has_the_legacy_discount_model(self):
        self.assertEqual(self.client.get('/admin/products/discount/').status_code, 404)


# ======================================================================== دستور مدیریتی
class RefreshCommandTests(PromotionsTestBase):
    def test_command_rebuilds_the_index(self):
        from io import StringIO
        from django.core.management import call_command
        make_promotion(self.p_root)
        out = StringIO()
        call_command('refresh_promotions_cache', stdout=out)
        self.assertIn('1 تخفیف فعال‌شده', out.getvalue())
        self.assertIn('روشن', out.getvalue())
        self.set_policy(promotions_enabled=False)
        out = StringIO()
        call_command('refresh_promotions_cache', stdout=out)
        self.assertIn('خاموش', out.getvalue())


# ======================================================================== مهاجرت تخفیف‌های قدیمی
class LegacyDiscountMigrationTests(TransactionTestCase):
    """ products.Discount ← promotions.Promotion (عیناً) و حذف جدول قدیمی؛ به‌همراه برگشت (rollback) """

    serialized_rollback = True
    BEFORE = [('products', '0020_remove_sitesettings_shipping_cost'), ('promotions', '0001_initial')]

    def migrate(self, targets):
        MigrationExecutor(connection).migrate(targets)
        return MigrationExecutor(connection).loader.project_state(targets).apps

    def tearDown(self):
        MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
        reset_promotions_cache()

    def make_old_discounts(self):
        old = self.migrate(self.BEFORE)                     # جدول Discount وجود دارد، promotions هنوز خالی
        Category_, Product_, Discount_ = (old.get_model('products', n) for n in ('Category', 'Product', 'Discount'))
        category = Category_.objects.create(name='مهاجرت', slug=f'mig-cat-{next(_seq)}')
        products = [Product_.objects.create(name=f'کالای مهاجرت {i}', slug=f'mig-p-{next(_seq)}', erp_code=f'MIG-{next(_seq)}',
                                            category=category, price=100000, stock=1) for i in range(3)]
        now = timezone.now()
        discounts = [
            Discount_.objects.create(product=products[0], percent=10, starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=1), is_active=True),
            Discount_.objects.create(product=products[1], percent=40, starts_at=now + timedelta(days=1), ends_at=now + timedelta(days=5), is_active=False),
            Discount_.objects.create(product=products[2], percent=25, starts_at=now - timedelta(days=9), ends_at=now - timedelta(days=2), is_active=True),
        ]
        return products, discounts

    def test_forward_copies_every_discount_exactly_and_drops_the_old_table(self):
        products, discounts = self.make_old_discounts()
        new = self.migrate([('products', '0021_delete_discount'), ('promotions', '0002_migrate_legacy_discounts')])
        Promotion_ = new.get_model('promotions', 'Promotion')
        self.assertEqual(Promotion_.objects.count(), 3)
        for old, product in zip(discounts, products):
            promo = Promotion_.objects.get(targets__product_id=product.pk)
            self.assertEqual((promo.kind, promo.value, promo.starts_at, promo.ends_at, promo.is_active),
                             ('percent', old.percent, old.starts_at, old.ends_at, old.is_active))
            self.assertTrue(promo.show_in_flash_deals)
            self.assertEqual(promo.targets.count(), 1)
            self.assertEqual(promo.targets.get().target_type, 'product')
            self.assertIn(f'{old.percent}٪', promo.title)
            self.assertNotRegex(promo.title, '[يك]')                         # املای فارسی، نه عربی
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'products_discount'")
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_migrated_promotions_price_like_the_old_discounts(self):
        products, _ = self.make_old_discounts()
        self.migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
        reset_promotions_cache()
        user = CustomUser.objects.create_user(phone_number=f'0912005{next(_seq):04d}', price_level=1)
        live = Product.objects.get(pk=products[0].pk)
        self.assertEqual(final_price(live, user, CHECK), Decimal('90000'))            # فعال ۱۰٪
        self.assertEqual(final_price(Product.objects.get(pk=products[1].pk), user, CHECK), Decimal('100000'))   # غیرفعال
        self.assertEqual(final_price(Product.objects.get(pk=products[2].pk), user, CHECK), Decimal('100000'))   # منقضی

    def test_rollback_restores_simple_discounts_and_reports_the_rest(self):
        products, discounts = self.make_old_discounts()
        new = self.migrate([('products', '0021_delete_discount'), ('promotions', '0002_migrate_legacy_discounts')])
        Promotion_, Target_ = new.get_model('promotions', 'Promotion'), new.get_model('promotions', 'PromotionTarget')
        now = timezone.now()
        complex_promo = Promotion_.objects.create(title='دسته‌ای', kind='percent', value=5, starts_at=now, ends_at=now + timedelta(days=1))
        Target_.objects.create(promotion=complex_promo, target_type='all')            # در مدل قدیمی بیان‌پذیر نیست

        old = self.migrate(self.BEFORE)
        Discount_ = old.get_model('products', 'Discount')
        self.assertEqual(Discount_.objects.count(), 3)                                # فقط سه تخفیف ساده برگشت
        self.assertEqual(sorted(Discount_.objects.values_list('percent', flat=True)), [10, 25, 40])
        self.assertEqual(Discount_.objects.get(percent=40).is_active, False)
