"""
تست‌های سیستم امتیاز/سطح وفاداری (accounts.models.CustomUser.get_loyalty_*) که از تنظیمات
SiteSettings.loyalty_* (اپ products) زنده می‌خوانند - نگاه کنید accounts/stats.py:get_config
و products/stats.py.

نکته‌ی مهم درباره‌ی کش: SiteSettings.cached() از کش مشترک (Redis/memcache) استفاده می‌کند که
rollbackِ تراکنش TestCase به آن سیگنال نمی‌فرستد (دقیقاً همان تله‌ای که promotions/testing.py
برای شاخص تخفیف مستند کرده). به همین دلیل هر تست این فایل کش تنظیمات را قبل/بعد پاک می‌کند و
تغییرات را با save() واقعی (نه queryset.update()) انجام می‌دهد تا سیگنال ابطال کش شلیک شود.
"""

import itertools

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import TestCase

from accounts.models import CustomUser
from orders.models import Order, OrderItem
from payments.models import Transaction
from products.models import SiteSettings

_authority_seq = itertools.count(1)


def _make_paid_order(user, *, item_price=None, item_qty=1, shipping_cost=0):
    """ یک سفارش با تراکنش پرداخت موفق؛ اگر item_price داده شود یک ردیف کالا هم دارد """
    items_total = (item_price or 0) * item_qty
    order = Order.objects.create(
        user=user, first_name='کاربر', last_name='تست', phone=user.phone_number,
        address='تهران', payment_method='cash', shipping_cost=shipping_cost,
        total_price=items_total + shipping_cost,
    )
    if item_price is not None:
        OrderItem.objects.create(order=order, price=item_price, quantity=item_qty)
    Transaction.objects.create(
        user=user, order=order, amount=order.total_price,
        authority=f'TEST-LOYALTY-{next(_authority_seq)}', status='success',
    )
    return order


class LoyaltyTestBase(TestCase):
    def setUp(self):
        super().setUp()
        cache.delete(SiteSettings.CACHE_KEY)
        SiteSettings.load().save()  # ردیف تنظیمات با مقادیر پیش‌فرض (و کش تازه)
        self.addCleanup(self._reset_settings)

    def _reset_settings(self):
        obj = SiteSettings.load()
        obj.loyalty_mode = SiteSettings.LOYALTY_MODE_ORDER_COUNT
        obj.loyalty_points_per_order = 100
        obj.loyalty_amount_step = 100000
        obj.loyalty_threshold_bronze = 300
        obj.loyalty_threshold_silver = 700
        obj.loyalty_threshold_gold = 1500
        obj.loyalty_threshold_diamond = 3000
        obj.save()
        cache.delete(SiteSettings.CACHE_KEY)

    def set_loyalty(self, **fields):
        """ تغییر SiteSettings.loyalty_* با save() واقعی، تا سیگنال ابطال کش شلیک شود """
        obj = SiteSettings.load()
        for name, value in fields.items():
            setattr(obj, name, value)
        obj.save()
        return obj

    def fresh(self, user):
        """ نمونه‌ی تازه‌ی کاربر، بدون هیچ cached_property باقی‌مانده از قبل (paid_orders_count/_loyalty_config) """
        return CustomUser.objects.get(pk=user.pk)


class OrderCountModeTests(LoyaltyTestBase):
    """ حالت پیش‌فرض: هر سفارش پرداخت‌شده = ۱۰۰ امتیاز (SiteSettings.loyalty_points_per_order) """

    def test_points_and_level_from_order_count(self):
        user = CustomUser.objects.create_user(phone_number='09120009001')
        for _ in range(7):
            _make_paid_order(user)
        user = self.fresh(user)
        self.assertEqual(user.get_loyalty_points(), 700)
        level, next_level, remaining = user.get_loyalty_level()
        self.assertEqual(level, 'نقره‌ای')          # ۷۰۰ == آستانه‌ی نقره‌ای
        self.assertEqual(next_level, 'طلایی')
        self.assertEqual(remaining, 800)             # تا ۱۵۰۰ (طلایی)
        self.assertEqual(user.get_loyalty_level_index(), 2)

    def test_custom_points_per_order(self):
        self.set_loyalty(loyalty_points_per_order=50)
        user = CustomUser.objects.create_user(phone_number='09120009002')
        for _ in range(6):
            _make_paid_order(user)
        user = self.fresh(user)
        self.assertEqual(user.get_loyalty_points(), 300)   # ۶ سفارش × ۵۰
        self.assertEqual(user.get_loyalty_level()[0], 'برنزی')

    def test_new_customer_below_first_threshold(self):
        user = CustomUser.objects.create_user(phone_number='09120009003')
        _make_paid_order(user)
        user = self.fresh(user)
        self.assertEqual(user.get_loyalty_points(), 100)
        self.assertEqual(user.get_loyalty_level()[0], 'مشتری جدید')
        self.assertEqual(user.get_loyalty_level_index(), 0)


class AmountModeTests(LoyaltyTestBase):
    """ حالت مبلغ خرید: هر loyalty_amount_step تومان مبلغ خالص اقلام = ۱ امتیاز """

    def test_points_from_net_item_amount(self):
        self.set_loyalty(loyalty_mode=SiteSettings.LOYALTY_MODE_AMOUNT, loyalty_amount_step=100000)
        user = CustomUser.objects.create_user(phone_number='09120009011')
        _make_paid_order(user, item_price=70_000_000, item_qty=1)
        user = self.fresh(user)
        self.assertEqual(user.get_loyalty_points(), 700)
        self.assertEqual(user.get_loyalty_level()[0], 'نقره‌ای')

    def test_shipping_cost_is_excluded_from_the_amount(self):
        """ هزینه‌ی ارسال نباید در محاسبه‌ی امتیاز دخیل شود، حتی اگر خیلی بزرگ باشد """
        self.set_loyalty(loyalty_mode=SiteSettings.LOYALTY_MODE_AMOUNT, loyalty_amount_step=100000)
        user = CustomUser.objects.create_user(phone_number='09120009012')
        _make_paid_order(user, item_price=1_000_000, item_qty=1, shipping_cost=999_999_999)
        user = self.fresh(user)
        self.assertEqual(user.get_loyalty_points(), 10)   # فقط ۱٬۰۰۰٬۰۰۰ اقلام، نه هزینه‌ی ارسال

    def test_multiple_paid_orders_are_summed(self):
        self.set_loyalty(loyalty_mode=SiteSettings.LOYALTY_MODE_AMOUNT, loyalty_amount_step=100000)
        user = CustomUser.objects.create_user(phone_number='09120009013')
        _make_paid_order(user, item_price=200000, item_qty=2)   # ۴۰۰٬۰۰۰
        _make_paid_order(user, item_price=100000, item_qty=3)   # ۳۰۰٬۰۰۰
        user = self.fresh(user)
        self.assertEqual(user.paid_net_amount, 700000)
        self.assertEqual(user.get_loyalty_points(), 7)


class LiveThresholdUpdateTests(LoyaltyTestBase):
    """ تصمیم کارفرما: محاسبه‌ی زنده - تغییر تنظیمات ادمین باید بلافاصله (بدون مهاجرت/بازمحاسبه) اثر کند """

    def test_lowering_a_threshold_upgrades_the_user_immediately(self):
        user = CustomUser.objects.create_user(phone_number='09120009021')
        for _ in range(5):
            _make_paid_order(user)   # ۵۰۰ امتیاز
        before = self.fresh(user)
        self.assertEqual(before.get_loyalty_level()[0], 'برنزی')   # ۵۰۰ زیر آستانه‌ی نقره‌ای پیش‌فرض (۷۰۰)

        self.set_loyalty(loyalty_threshold_silver=400)   # ادمین آستانه را پایین می‌آورد

        after = self.fresh(user)   # نمونه‌ی تازه؛ _loyalty_config/paid_orders_count قبلی cached_property بودند
        self.assertEqual(after.get_loyalty_points(), 500)   # امتیاز خام تغییر نکرده
        self.assertEqual(after.get_loyalty_level()[0], 'نقره‌ای')   # ولی سطح فوراً ارتقا یافته

    def test_switching_mode_changes_points_immediately(self):
        user = CustomUser.objects.create_user(phone_number='09120009022')
        _make_paid_order(user, item_price=70_000_000, item_qty=1)
        before = self.fresh(user)
        self.assertEqual(before.get_loyalty_points(), 100)   # حالت پیش‌فرض: ۱ سفارش × ۱۰۰

        self.set_loyalty(loyalty_mode=SiteSettings.LOYALTY_MODE_AMOUNT, loyalty_amount_step=100000)

        after = self.fresh(user)
        self.assertEqual(after.get_loyalty_points(), 700)   # حالا بر مبنای مبلغ


class LoyaltyThresholdConstraintTests(LoyaltyTestBase):
    """ کاندیشن‌های مدل SiteSettings برای فیلدهای loyalty_* """

    def test_non_ascending_thresholds_are_rejected_at_database_level(self):
        for change in (
            {'loyalty_threshold_silver': 100},                 # کمتر از برنزی (۳۰۰)
            {'loyalty_threshold_gold': 700},                    # مساوی نقره‌ای
            {'loyalty_threshold_diamond': 1000},                 # کمتر از طلایی (۱۵۰۰)
        ):
            with self.subTest(change=change), self.assertRaises(IntegrityError), transaction.atomic():
                SiteSettings.objects.filter(pk=1).update(**change)

    def test_non_positive_amount_step_or_points_per_order_are_rejected(self):
        for change in ({'loyalty_amount_step': 0}, {'loyalty_points_per_order': 0}):
            with self.subTest(change=change), self.assertRaises(IntegrityError), transaction.atomic():
                SiteSettings.objects.filter(pk=1).update(**change)

    def test_non_ascending_thresholds_are_rejected_by_clean(self):
        from django.core.exceptions import ValidationError
        obj = SiteSettings.load()
        obj.loyalty_threshold_silver = 100  # کمتر از برنزی
        with self.assertRaises(ValidationError) as caught:
            obj.full_clean()
        self.assertIn('loyalty_threshold_bronze', caught.exception.message_dict)

    def test_ascending_thresholds_are_accepted(self):
        obj = SiteSettings.load()
        obj.loyalty_threshold_bronze = 100
        obj.loyalty_threshold_silver = 200
        obj.loyalty_threshold_gold = 300
        obj.loyalty_threshold_diamond = 400
        obj.full_clean()
        obj.save()   # نباید هیچ خطایی بدهد
