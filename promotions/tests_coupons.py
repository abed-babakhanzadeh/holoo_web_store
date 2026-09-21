"""
تست کدهای تخفیف: نرمال‌سازی، مدل، ماتریس ارزیابی (نوع/شمول/حداقل سبد/ترکیب با تخفیف خودکار/مخاطب/زمان/سقف‌ها)،
چرخه‌ی رزرو ← مصرف/آزادسازی، سیگنال‌های سفارش و پرداخت، و محدودکننده‌ی حدس کد.
"""

import itertools
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from accounts.models import CustomUser
from cart.models import Cart, CartItem
from cart.pricing import price_cart
from orders.models import Order
from orders.shipping import ShippingQuote
from payments.signals import payment_succeeded
from products.models import Category, Product
from products.pricing import CHECK

from . import coupons, ratelimit
from .coupons import evaluate_coupon
from .models import Coupon, CouponRedemption, DiscountPolicy, UserCoupon, generate_code, normalize_code
from .testing import PromotionTestMixin, TEST_CLIENT_IP, make_coupon, make_promotion, reset_coupon_attempts

_seq = itertools.count(1)


class CouponBase(PromotionTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        DiscountPolicy.load()
        self.root = Category.objects.create(name='ریشه کوپن', slug=f'coupon-root-{next(_seq)}')
        self.child = Category.objects.create(name='فرزند کوپن', slug=f'coupon-child-{next(_seq)}', parent=self.root)
        self.other = Category.objects.create(name='دسته‌ی دیگر', slug=f'coupon-other-{next(_seq)}')
        self.user = self.new_user()
        self.cart = Cart.objects.create(user=self.user)
        self.p1 = self.product('کالای ۱', self.root)
        self.p2 = self.product('کالای ۲', self.child)
        self.p3 = self.product('کالای ۳', self.other)
        reset_coupon_attempts(self.user)

    def new_user(self, **fields):
        return CustomUser.objects.create_user(phone_number=f'0912011{next(_seq):04d}', price_level=fields.pop('price_level', 1), **fields)

    def product(self, name, category, price=100000, **extra):
        n = next(_seq)
        data = dict(name=name, slug=f'coupon-p-{n}', erp_code=f'ERP-CPN-{n}', category=category, price=price, price2=price - 10000, stock=50)
        data.update(extra)
        return Product.objects.create(**data)

    def add(self, product, quantity=1):
        return CartItem.objects.create(cart=self.cart, product=product, quantity=quantity)

    def pricing(self, user=None, now=None):
        items = list(self.cart.items.select_related('product').order_by('pk'))
        return price_cart(items, user or self.user, CHECK, now)

    def evaluate(self, coupon, user=None, quote=None, now=None):
        user = user or self.user
        return evaluate_coupon(coupon, user, self.pricing(user), base_quote=quote, now=now)

    def quote(self, method='courier', cost=45000, available=True):
        return ShippingQuote(available, method if available else '', cost, 'ارسال با پیک', '' if available else 'no_address', '', False)

    def make_order(self, user=None, **fields):
        data = dict(user=user or self.user, first_name='الف', last_name='ب', phone='09120000000', address='x', total_price=100000)
        data.update(fields)
        return Order.objects.create(**data)

    def redeem_row(self, coupon, user=None, status=CouponRedemption.STATUS_REDEEMED, expires_in=None, order=None, amount=1000):
        order = order or self.make_order(user)
        expires_at = timezone.now() + (expires_in if expires_in is not None else timedelta(minutes=30))
        return CouponRedemption.objects.create(coupon=coupon, user=user or self.user, order_id=order.pk, code=coupon.code,
                                               status=status, discount_amount=amount, expires_at=expires_at)


# ======================================================================== کد و مدل
class NormalizeCodeTests(TestCase):
    def test_case_persian_and_arabic_digits_spaces_and_dashes(self):
        self.assertEqual(normalize_code('  save-۱۰ '), 'SAVE-10')
        self.assertEqual(normalize_code('yalda٢٠٢٥'), 'YALDA2025')
        self.assertEqual(normalize_code('a b‌c'), 'ABC')
        self.assertEqual(normalize_code('AB–12'), 'AB-12')
        self.assertEqual(normalize_code(None), '')
        self.assertEqual(normalize_code('   '), '')

    def test_generated_codes_use_the_unambiguous_alphabet_and_prefix(self):
        for _ in range(200):
            code = generate_code('yalda', 8)
            self.assertRegex(code, r'^YALDA-[A-HJKMNP-Z2-9]{8}$')
        self.assertRegex(generate_code(), r'^[A-HJKMNP-Z2-9]{8}$')
        self.assertNotRegex(generate_code('', 16), '[01OIL]')

    def test_generated_codes_are_random(self):
        self.assertEqual(len({generate_code('X', 10) for _ in range(500)}), 500)


class CouponModelTests(CouponBase):
    def test_code_is_normalised_on_save_and_lookup_is_case_insensitive(self):
        coupon = make_coupon('save۱۰')
        self.assertEqual(coupon.code, 'SAVE10')
        for typed in ('save10', 'SAVE10', ' Save۱۰ ', 'sAvE١٠'):
            self.assertEqual(coupons.find_coupon(typed), coupon, typed)
        self.assertIsNone(coupons.find_coupon('SAVE11'))
        self.assertIsNone(coupons.find_coupon(''))

    def test_blank_code_is_generated_and_unique(self):
        first = Coupon.objects.create(title='خودکار', kind='percent', value=5, code='')
        second = Coupon.objects.create(title='خودکار۲', kind='percent', value=5, code='')
        self.assertNotEqual(first.code, second.code)
        self.assertRegex(first.code, r'^[A-Z0-9]{8}$')

    def test_duplicate_code_in_another_case_is_rejected(self):
        make_coupon('DUP1')
        with self.assertRaises(Exception):
            make_coupon('dup1')

    def assertInvalid(self, coupon, field):
        with self.assertRaises(ValidationError) as ctx:
            coupon.full_clean(exclude=['products', 'categories'])
        self.assertIn(field, ctx.exception.message_dict)

    def test_validation_rules(self):
        now = timezone.now()
        self.assertInvalid(Coupon(code='A1', title='t', kind='percent', value=0), 'value')
        self.assertInvalid(Coupon(code='A1', title='t', kind='percent', value=101), 'value')
        self.assertInvalid(Coupon(code='A1', title='t', kind='fixed', value=0), 'value')
        self.assertInvalid(Coupon(code='A1', title='t', kind='fixed', value=10, max_discount_amount=5), 'max_discount_amount')
        self.assertInvalid(Coupon(code='A1B2', title='t', kind='percent', value=10, starts_at=now, ends_at=now), 'ends_at')
        self.assertInvalid(Coupon(code='A1B2', title='t', kind='percent', value=10, total_limit=0), 'total_limit')
        self.assertInvalid(Coupon(code='A1B2', title='t', kind='percent', value=10, per_user_limit=0), 'per_user_limit')
        self.assertInvalid(Coupon(code='A1B2', title='t', kind='free_shipping', scope='items'), 'scope')
        self.assertInvalid(Coupon(code='بد!', title='t', kind='percent', value=10), 'code')
        Coupon(code='GOOD-1', title='t', kind='percent', value=10, max_discount_amount=50000).full_clean(exclude=['products', 'categories'])
        Coupon(code='SHIP-1', title='t', kind='free_shipping', value=0).full_clean(exclude=['products', 'categories'])

    def test_status_and_open_ended_window(self):
        self.assertEqual(make_coupon('ST1').status(), 'active')                         # بدون تاریخ = همیشه
        self.assertEqual(make_coupon('ST2', active=False).status(), 'inactive')
        self.assertEqual(make_coupon('ST3', expired=True).status(), 'expired')
        self.assertEqual(make_coupon('ST4', scheduled=True).status(), 'scheduled')
        self.assertEqual(make_coupon('ST5').status_label, 'فعال')

    def test_value_display(self):
        self.assertEqual(make_coupon('D1', value=25, max_discount_amount=50000).value_display, '25٪ (حداکثر 50,000 تومان)')
        self.assertEqual(make_coupon('D2', kind='fixed', value=15000).value_display, '15,000 تومان')
        self.assertEqual(make_coupon('D3', kind='free_shipping').value_display, 'ارسال رایگان')


# ======================================================================== ماتریس ارزیابی
class EvaluationTests(CouponBase):
    def setUp(self):
        super().setUp()
        self.add(self.p1, 2)
        self.add(self.p2, 1)                                                   # ۳۰۰٬۰۰۰ کل

    def test_percent_on_the_whole_cart(self):
        result = self.evaluate(make_coupon('P20', value=20))
        self.assertTrue(result.ok)
        self.assertEqual((result.item_discount, result.eligible_total, result.free_shipping), (60000, 300000, False))
        self.assertEqual((result.code, result.kind), ('P20', 'percent'))
        self.assertEqual(result.order_label, 'کد تخفیف P20')

    def test_percent_is_capped(self):
        self.assertEqual(self.evaluate(make_coupon('P50', value=50, max_discount_amount=40000)).item_discount, 40000)

    def test_percent_rounds_down_to_whole_units(self):
        product = self.product('گرد', self.other, price=33333)
        self.cart.items.all().delete()
        self.add(product, 1)
        result = self.evaluate(make_coupon('P33', value=33))
        self.assertEqual(result.item_discount, 10999)                          # ۳۳٬۳۳۳ × ۰٫۳۳ = ۱۰٬۹۹۹٫۸۹ ← کف

    def test_fixed_amount_and_it_never_exceeds_the_eligible_amount(self):
        self.assertEqual(self.evaluate(make_coupon('F1', kind='fixed', value=25000)).item_discount, 25000)
        self.assertEqual(self.evaluate(make_coupon('F2', kind='fixed', value=10 ** 7)).item_discount, 300000)

    def test_hundred_percent_covers_the_goods_but_not_more(self):
        self.assertEqual(self.evaluate(make_coupon('P100', value=100)).item_discount, 300000)

    def test_scope_items_by_product(self):
        result = self.evaluate(make_coupon('SP', value=10, products=[self.p1]))
        self.assertEqual((result.eligible_total, result.item_discount), (200000, 20000))

    def test_scope_items_by_category_includes_descendants_but_not_parents(self):
        self.assertEqual(self.evaluate(make_coupon('SC1', value=10, categories=[self.root])).eligible_total, 300000)   # p1 و نوه
        self.assertEqual(self.evaluate(make_coupon('SC2', value=10, categories=[self.child])).eligible_total, 100000)  # فقط فرزند

    def test_scope_items_with_no_matching_line_is_not_applicable(self):
        result = self.evaluate(make_coupon('SN', value=10, products=[self.p3]))
        self.assertFalse(result.ok)
        self.assertEqual(result.error, coupons.NOT_APPLICABLE)
        self.assertIn('مشمول', result.message)

    def test_product_and_category_scopes_combine_with_or(self):
        result = self.evaluate(make_coupon('SOR', value=10, products=[self.p1], categories=[self.child]))
        self.assertEqual(result.eligible_total, 300000)

    # ----- حداقل مبلغ سبد پس از تخفیف خودکار -----
    def test_minimum_cart_amount(self):
        result = self.evaluate(make_coupon('MIN1', min_cart_amount=300001))
        self.assertEqual((result.ok, result.error), (False, coupons.MIN_CART))
        self.assertIn('300001', result.message)
        self.assertTrue(self.evaluate(make_coupon('MIN2', min_cart_amount=300000)).ok)              # مرز: مساوی مجاز

    def test_minimum_is_measured_after_automatic_discounts(self):
        make_promotion(self.p1, percent=50)                                    # ۲۰۰٬۰۰۰ ← ۱۰۰٬۰۰۰؛ سبد ۲۰۰٬۰۰۰
        self.assertEqual(self.pricing().items_total, 200000)
        self.assertEqual(self.evaluate(make_coupon('MIN3', min_cart_amount=250000)).error, coupons.MIN_CART)
        self.assertTrue(self.evaluate(make_coupon('MIN4', min_cart_amount=200000, allow_with_promotions=True)).ok)

    # ----- ترکیب با تخفیف خودکار -----
    def test_by_default_a_coupon_skips_lines_that_have_an_automatic_discount(self):
        make_promotion(self.p1, percent=50)                                    # p1: ۲×۵۰٬۰۰۰ = ۱۰۰٬۰۰۰ با تخفیف؛ p2: ۱۰۰٬۰۰۰ بدون
        result = self.evaluate(make_coupon('NC1', value=10))
        self.assertTrue(result.ok)
        self.assertEqual((result.eligible_total, result.item_discount, result.excluded_lines), (100000, 10000, 1))

    def test_default_rule_errors_clearly_when_every_line_has_an_automatic_discount(self):
        make_promotion(self.p1, percent=10)
        make_promotion(self.p2, percent=10)
        result = self.evaluate(make_coupon('NC2', value=10))
        self.assertFalse(result.ok)
        self.assertEqual(result.error, coupons.NO_COMBINE)
        self.assertIn('تخفیف خودکار', result.message)

    def test_allow_with_promotions_applies_on_the_discounted_prices(self):
        make_promotion(self.p1, percent=50)
        result = self.evaluate(make_coupon('NC3', value=10, allow_with_promotions=True))
        self.assertEqual((result.eligible_total, result.item_discount, result.excluded_lines), (200000, 20000, 0))

    def test_no_combine_rule_never_blocks_a_free_shipping_coupon(self):
        make_promotion(self.p1, percent=10)
        make_promotion(self.p2, percent=10)
        self.assertTrue(self.evaluate(make_coupon('NC4', kind='free_shipping'), quote=self.quote()).ok)

    # ----- زمان و فعال بودن -----
    def test_inactive_scheduled_and_expired(self):
        self.assertEqual(self.evaluate(make_coupon('W1', active=False)).error, coupons.INACTIVE)
        self.assertEqual(self.evaluate(make_coupon('W2', scheduled=True)).error, coupons.NOT_STARTED)
        self.assertEqual(self.evaluate(make_coupon('W3', expired=True)).error, coupons.EXPIRED)

    def test_window_boundaries_are_inclusive_and_use_the_given_server_time(self):
        now = timezone.now()
        coupon = make_coupon('W4', starts_at=now, ends_at=now + timedelta(hours=1))
        self.assertTrue(self.evaluate(coupon, now=coupon.starts_at).ok)
        self.assertTrue(self.evaluate(coupon, now=coupon.ends_at).ok)
        self.assertEqual(self.evaluate(coupon, now=coupon.starts_at - timedelta(microseconds=1)).error, coupons.NOT_STARTED)
        self.assertEqual(self.evaluate(coupon, now=coupon.ends_at + timedelta(microseconds=1)).error, coupons.EXPIRED)

    def test_unknown_coupon(self):
        result = evaluate_coupon(None, self.user, self.pricing())
        self.assertEqual((result.ok, result.error), (False, coupons.NOT_FOUND))

    # ----- مخاطب و اولین خرید -----
    def test_assigned_only_coupon(self):
        coupon = make_coupon('AUD1', audience='assigned')
        self.assertEqual(self.evaluate(coupon).error, coupons.NOT_ASSIGNED)
        UserCoupon.objects.create(coupon=coupon, user=self.user)
        self.assertTrue(self.evaluate(coupon).ok)
        other = self.new_user()
        self.assertEqual(self.evaluate(coupon, user=other).error, coupons.NOT_ASSIGNED)

    def test_first_order_only(self):
        coupon = make_coupon('FIRST1', first_order_only=True)
        self.assertTrue(self.evaluate(coupon).ok)
        placed = self.make_order()
        result = self.evaluate(coupon)
        self.assertEqual((result.ok, result.error), (False, coupons.FIRST_ORDER_ONLY))
        placed.status = 'canceled'
        placed.save()                                                           # سفارش لغوشده «خرید قبلی» حساب نمی‌شود
        self.assertTrue(self.evaluate(coupon).ok)

    def test_first_order_only_fails_closed_when_the_stat_is_unavailable(self):
        coupon = make_coupon('FIRST2', first_order_only=True)
        with mock.patch('promotions.coupons.get_stat', return_value=None):
            self.assertEqual(self.evaluate(coupon).error, coupons.FIRST_ORDER_UNKNOWN)

    # ----- سقف‌ها -----
    def test_total_limit_counts_redeemed_and_live_reservations_only(self):
        coupon = make_coupon('LIM1', total_limit=2, per_user_limit=None)
        self.assertTrue(self.evaluate(coupon).ok)
        self.redeem_row(coupon, self.new_user())
        self.assertTrue(self.evaluate(coupon).ok)
        self.redeem_row(coupon, self.new_user(), status='reserved')
        result = self.evaluate(coupon)
        self.assertEqual((result.ok, result.error), (False, coupons.EXHAUSTED))

    def test_expired_reservations_and_released_rows_do_not_use_capacity(self):
        coupon = make_coupon('LIM2', total_limit=1, per_user_limit=None)
        self.redeem_row(coupon, self.new_user(), status='reserved', expires_in=timedelta(seconds=-1))    # مهلت گذشته
        self.redeem_row(coupon, self.new_user(), status='released')
        self.assertEqual(coupons.active_uses(coupon), 0)
        self.assertTrue(self.evaluate(coupon).ok)

    def test_per_user_limit_is_per_user(self):
        coupon = make_coupon('LIM3', per_user_limit=1)
        self.redeem_row(coupon, self.user)
        self.assertEqual(self.evaluate(coupon).error, coupons.USER_LIMIT)
        self.assertTrue(self.evaluate(coupon, user=self.new_user()).ok)

    def test_unlimited_when_limits_are_empty(self):
        coupon = make_coupon('LIM4', total_limit=None, per_user_limit=None)
        for _ in range(5):
            self.redeem_row(coupon, self.user)
        self.assertTrue(self.evaluate(coupon).ok)

    def test_per_user_limit_two(self):
        coupon = make_coupon('LIM5', per_user_limit=2)
        self.redeem_row(coupon, self.user)
        self.assertTrue(self.evaluate(coupon).ok)
        self.redeem_row(coupon, self.user)
        self.assertEqual(self.evaluate(coupon).error, coupons.USER_LIMIT)

    # ----- ارسال رایگان -----
    def test_free_shipping_needs_an_address_quote(self):
        coupon = make_coupon('SHIP1', kind='free_shipping')
        self.assertEqual(self.evaluate(coupon).error, coupons.NEEDS_ADDRESS)
        self.assertEqual(self.evaluate(coupon, quote=self.quote(available=False)).error, coupons.NEEDS_ADDRESS)

    def test_free_shipping_only_for_a_courier_with_a_tariff(self):
        coupon = make_coupon('SHIP2', kind='free_shipping')
        self.assertEqual(self.evaluate(coupon, quote=self.quote(method='post', cost=0)).error, coupons.SHIPPING_NOT_APPLICABLE)
        self.assertEqual(self.evaluate(coupon, quote=self.quote(cost=0)).error, coupons.SHIPPING_ALREADY_FREE)
        result = self.evaluate(coupon, quote=self.quote(cost=45000))
        self.assertTrue(result.ok)
        self.assertEqual((result.shipping_discount, result.item_discount, result.free_shipping), (45000, 0, True))

    def test_free_shipping_still_respects_min_cart_and_limits(self):
        coupon = make_coupon('SHIP3', kind='free_shipping', min_cart_amount=500000)
        self.assertEqual(self.evaluate(coupon, quote=self.quote()).error, coupons.MIN_CART)
        limited = make_coupon('SHIP4', kind='free_shipping', total_limit=1, per_user_limit=None)
        self.redeem_row(limited, self.new_user())
        self.assertEqual(self.evaluate(limited, quote=self.quote()).error, coupons.EXHAUSTED)

    def test_evaluation_has_no_side_effects(self):
        coupon = make_coupon('PURE')
        before = CouponRedemption.objects.count()
        self.evaluate(coupon)
        self.assertEqual(CouponRedemption.objects.count(), before)

    def test_guess_failures_are_only_the_code_probing_errors(self):
        guesses = {coupons.NOT_FOUND, coupons.INACTIVE, coupons.NOT_STARTED, coupons.EXPIRED, coupons.NOT_ASSIGNED}
        self.assertEqual(coupons.GUESS_ERRORS, guesses)
        self.assertTrue(evaluate_coupon(None, self.user, self.pricing()).is_guess_failure)
        self.assertFalse(self.evaluate(make_coupon('NG', min_cart_amount=10 ** 9)).is_guess_failure)


# ======================================================================== چرخه‌ی عمر
class RedemptionLifecycleTests(CouponBase):
    def setUp(self):
        super().setUp()
        self.add(self.p1, 1)
        self.coupon = make_coupon('LIFE', total_limit=1, per_user_limit=None)
        self.order = self.make_order()
        self.result = self.evaluate(self.coupon)

    def reserve(self, order=None, now=None):
        return coupons.reserve(self.coupon, self.user, (order or self.order).pk, self.result, now)

    def test_reserve_snapshots_amounts_and_sets_the_deadline_from_policy(self):
        now = timezone.now()
        redemption = self.reserve(now=now)
        self.assertEqual((redemption.status, redemption.code, redemption.discount_amount, redemption.order_id),
                         ('reserved', 'LIFE', 10000, self.order.pk))
        self.assertEqual(redemption.expires_at, now + timedelta(minutes=30))
        policy = DiscountPolicy.load()
        policy.coupon_reservation_minutes = 45
        policy.save()
        second = coupons.reserve(self.coupon, self.user, self.make_order().pk, self.result, now)
        self.assertEqual(second.expires_at, now + timedelta(minutes=45))

    def test_one_redemption_per_order(self):
        self.reserve()
        with self.assertRaises(Exception):
            self.reserve()

    def test_reservation_takes_capacity_until_it_expires(self):
        now = timezone.now()
        self.reserve(now=now)
        self.assertEqual(coupons.active_uses(self.coupon, now=now + timedelta(minutes=29)), 1)
        self.assertEqual(coupons.active_uses(self.coupon, now=now + timedelta(minutes=31)), 0)
        self.assertEqual(self.evaluate(self.coupon, user=self.new_user(), now=now + timedelta(minutes=31)).ok, True)
        self.assertEqual(self.evaluate(self.coupon, user=self.new_user(), now=now + timedelta(minutes=1)).error, coupons.EXHAUSTED)

    def test_payment_success_turns_the_reservation_into_a_final_use(self):
        self.reserve()
        redemption = coupons.redeem_for_order(self.order.pk)
        self.assertEqual((redemption.status, redemption.over_limit), ('redeemed', False))
        self.assertIsNotNone(redemption.redeemed_at)
        self.assertEqual(coupons.redeem_for_order(self.order.pk).status, 'redeemed')            # idempotent
        self.assertEqual(coupons.active_uses(self.coupon), 1)

    def test_redeeming_an_order_without_a_coupon_is_a_no_op(self):
        self.assertIsNone(coupons.redeem_for_order(self.make_order().pk))

    def test_payment_after_the_reservation_expired_still_redeems_when_capacity_remains(self):
        now = timezone.now()
        self.reserve(now=now - timedelta(hours=2))                                              # مهلتش گذشته
        redemption = coupons.redeem_for_order(self.order.pk, now=now)
        self.assertEqual((redemption.status, redemption.over_limit), ('redeemed', False))

    def test_payment_after_expiry_over_the_limit_is_honoured_and_flagged(self):
        now = timezone.now()
        self.reserve(now=now - timedelta(hours=2))
        coupons.release_expired(now)
        other = self.make_order(self.new_user())
        self.redeem_row(self.coupon, other.user, order=other)                                   # کس دیگری ظرفیت را گرفت
        redemption = coupons.redeem_for_order(self.order.pk, now=now)
        self.assertEqual((redemption.status, redemption.over_limit), ('redeemed', True))
        self.assertIsNone(redemption.released_at)

    def test_release_from_reserved_and_from_redeemed(self):
        self.reserve()
        self.assertEqual(coupons.release_for_order(self.order.pk, 'order_canceled'), 1)
        redemption = CouponRedemption.objects.get(order_id=self.order.pk)
        self.assertEqual((redemption.status, redemption.release_reason), ('released', 'order_canceled'))
        self.assertEqual(coupons.active_uses(self.coupon), 0)
        self.assertEqual(coupons.release_for_order(self.order.pk), 0)                            # idempotent

        paid = self.make_order()
        coupons.reserve(self.coupon, self.user, paid.pk, self.result)
        coupons.redeem_for_order(paid.pk)
        self.assertEqual(coupons.release_for_order(paid.pk), 1)                                  # سفارش پرداخت‌شده هم اگر لغو شود
        self.assertEqual(coupons.active_uses(self.coupon), 0)

    def test_release_expired_only_touches_unpaid_overdue_reservations(self):
        now = timezone.now()
        overdue = self.reserve(now=now - timedelta(hours=2))
        fresh = coupons.reserve(self.coupon, self.user, self.make_order().pk, self.result, now)
        paid_order = self.make_order()
        paid = coupons.reserve(self.coupon, self.user, paid_order.pk, self.result, now - timedelta(hours=2))
        coupons.redeem_for_order(paid_order.pk, now=now - timedelta(hours=1, minutes=59))
        self.assertEqual(coupons.release_expired(now), 1)
        for row in (overdue, fresh, paid):
            row.refresh_from_db()
        self.assertEqual((overdue.status, overdue.release_reason), ('released', 'expired'))
        self.assertEqual((fresh.status, paid.status), ('reserved', 'redeemed'))

    def test_payment_signal_redeems_and_cancel_signal_releases(self):
        self.reserve()
        # گیرنده‌ی کوپن روی هر دو سیگنال ثبت است؛ برای پرداخت مستقیم صدایش می‌زنیم (سیگنال واقعی گیرنده‌های اعلان/هلوی
        # دیگر هم دارد که اینجا موضوع تست نیستند و به صف/پیامک واقعی می‌رسند)
        from orders.signals import order_canceled
        registered = {key[0] for key, *_ in payment_succeeded.receivers} | {key[0] for key, *_ in order_canceled.receivers}
        self.assertIn('promotions_redeem_coupon_on_payment', registered)
        self.assertIn('promotions_release_coupon_on_cancel', registered)
        from .signals import redeem_coupon_on_payment
        redeem_coupon_on_payment(sender=None, order=self.order, transaction=None)
        self.assertEqual(CouponRedemption.objects.get(order_id=self.order.pk).status, 'redeemed')
        with self.captureOnCommitCallbacks(execute=True):
            self.order.status = 'canceled'
            self.order.save()
        redemption = CouponRedemption.objects.get(order_id=self.order.pk)
        self.assertEqual((redemption.status, redemption.release_reason), ('released', 'order_canceled'))

    def test_only_a_real_cancellation_fires_the_cancel_signal(self):
        received = []
        from orders.signals import order_canceled
        order_canceled.connect(lambda sender, order, **kw: received.append(order.pk), dispatch_uid='probe', weak=False)
        self.addCleanup(order_canceled.disconnect, dispatch_uid='probe')
        with self.captureOnCommitCallbacks(execute=True):
            self.order.status = 'shipped'
            self.order.save()                                                                    # لغو نیست
        self.assertEqual(received, [])
        with self.captureOnCommitCallbacks(execute=True):
            self.order.status = 'canceled'
            self.order.save()
            self.order.save()                                                                    # ذخیره‌ی دوباره: سیگنال دوباره نمی‌رود
        self.assertEqual(received, [self.order.pk])
        fresh = Order.objects.get(pk=self.order.pk)                                              # خواندن دوباره
        with self.captureOnCommitCallbacks(execute=True):
            fresh.save()
        self.assertEqual(received, [self.order.pk])

    def test_cancel_signal_is_not_sent_when_the_transaction_rolls_back(self):
        from django.db import transaction
        received = []
        from orders.signals import order_canceled
        order_canceled.connect(lambda sender, order, **kw: received.append(order.pk), dispatch_uid='probe2', weak=False)
        self.addCleanup(order_canceled.disconnect, dispatch_uid='probe2')
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    self.order.status = 'canceled'
                    self.order.save()
                    raise RuntimeError('rollback')
            except RuntimeError:
                pass
        self.assertEqual(received, [])

    def test_management_command_and_task(self):
        self.reserve(now=timezone.now() - timedelta(hours=3))
        out = StringIO()
        call_command('release_expired_coupons', stdout=out)
        self.assertIn('1 رزرو منقضی آزاد شد', out.getvalue())
        from .tasks import release_expired_coupon_reservations
        self.assertEqual(release_expired_coupon_reservations(), 0)


# ======================================================================== محدودکننده‌ی حدس کد
class RateLimitTests(CouponBase):
    def setUp(self):
        super().setUp()
        self.other_user = self.new_user()
        reset_coupon_attempts(self.other_user)

    def fail(self, times, user=None, ip=TEST_CLIENT_IP):
        for _ in range(times):
            ratelimit.record_failure(user or self.user, ip)

    def test_ten_failures_block_the_eleventh_attempt_by_default(self):
        self.fail(9)
        self.assertTrue(ratelimit.check(self.user, TEST_CLIENT_IP)[0])
        self.fail(1)
        allowed, retry_after = ratelimit.check(self.user, TEST_CLIENT_IP)
        self.assertFalse(allowed)
        self.assertTrue(0 < retry_after <= 3600)

    def test_user_and_ip_are_counted_independently(self):
        self.fail(10, ip='')                                                       # فقط کاربر
        self.assertFalse(ratelimit.check(self.user, TEST_CLIENT_IP)[0])
        self.assertTrue(ratelimit.check(self.other_user, '198.51.100.9')[0])       # کاربر و IP دیگر آزادند
        self.fail(10, user=self.other_user, ip='198.51.100.9')
        self.assertFalse(ratelimit.check(self.new_user(), '198.51.100.9')[0])      # حتی کاربر تازه از همان IP مسدود است
        ratelimit.reset(None, '198.51.100.9')

    def test_cap_and_window_come_from_the_policy(self):
        policy = DiscountPolicy.load()
        policy.coupon_max_invalid_attempts = 3
        policy.coupon_attempt_window_minutes = 5
        policy.save()
        self.fail(3)
        allowed, retry_after = ratelimit.check(self.user, TEST_CLIENT_IP)
        self.assertFalse(allowed)
        self.assertTrue(0 < retry_after <= 300)

    def test_block_lifts_after_the_window(self):
        self.fail(10)
        self.assertFalse(ratelimit.check(self.user, TEST_CLIENT_IP)[0])
        with mock.patch('promotions.ratelimit.time.time', return_value=__import__('time').time() + 3601):
            self.assertTrue(ratelimit.check(self.user, TEST_CLIENT_IP)[0])
            self.fail(1)                                                           # پنجره‌ی تازه از یک شروع می‌شود
            self.assertTrue(ratelimit.check(self.user, TEST_CLIENT_IP)[0])

    def test_reset_clears_counters(self):
        self.fail(10)
        ratelimit.reset(self.user, TEST_CLIENT_IP)
        self.assertTrue(ratelimit.check(self.user, TEST_CLIENT_IP)[0])

    def test_cache_outage_fails_open_and_never_raises(self):
        with mock.patch('promotions.ratelimit.cache.get', side_effect=ConnectionError('down')), \
                mock.patch('promotions.ratelimit.cache.set', side_effect=ConnectionError('down')):
            self.assertEqual(ratelimit.check(self.user, TEST_CLIENT_IP), (True, 0))
            ratelimit.record_failure(self.user, TEST_CLIENT_IP)
