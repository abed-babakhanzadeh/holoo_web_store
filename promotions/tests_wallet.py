"""
تست پنل کدهای تخفیف کاربر (مرحله‌ی ۴): «کدهای من» (تب فعال / استفاده‌شده / منقضی-تمام‌شده) و «دریافت کد جدید».

محورها: وضعیت هر کد، طبقه‌بندی تب‌ها، متن قوانین، ایزولاسیون کاربران و عدم نشت کدهای اختصاصی، دریافت (یکتایی، سقف
دریافت‌کنندگان، سقف مصرف، واجد‌شرایط‌بودن)، پاسخ یکسان برای هر حالت غیرقابل‌دریافت، و صفحه‌های HTML/HTMX.
"""

import itertools
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomUser
from cart.models import Cart, CartItem
from cart.pricing import price_cart
from orders.models import Order
from products.models import Category, Product
from products.pricing import CHECK

from . import coupons, wallet
from .models import Coupon, CouponRedemption, DiscountPolicy, UserCoupon
from .testing import PromotionTestMixin, make_coupon, reset_coupon_attempts

_seq = itertools.count(1)
MY = 'promotions:my_codes'
GET = 'promotions:get_code'
CLAIM = 'promotions:claim'


class WalletBase(PromotionTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        DiscountPolicy.load()
        self.user = self.new_user()
        self.other = self.new_user()
        self.client.force_login(self.user)
        reset_coupon_attempts(self.user, self.other)
        self.category = Category.objects.create(name='کیف کد', slug=f'wallet-cat-{next(_seq)}')

    def new_user(self, **fields):
        return CustomUser.objects.create_user(phone_number=f'0912014{next(_seq):04d}', price_level=1, **fields)

    def product(self, name='کالای کیف', price=100000):
        n = next(_seq)
        return Product.objects.create(name=name, slug=f'wallet-p-{n}', erp_code=f'ERP-W-{n}', category=self.category, price=price, stock=9)

    def claimable(self, code='CLAIM1', **fields):
        fields.setdefault('is_claimable', True)
        fields.setdefault('title', f'کمپین قابل‌دریافت {next(_seq)}')          # عنوانِ پیش‌فرضِ make_coupon متن کد را دارد و پنهان می‌شود
        return make_coupon(code, **fields)

    def give(self, coupon, user=None, source='admin'):
        return UserCoupon.objects.create(coupon=coupon, user=user or self.user, source=source)

    def order(self, user=None, total=250000):
        return Order.objects.create(user=user or self.user, first_name='الف', last_name='ب', phone='09120000000', address='x', total_price=total)

    def redemption(self, coupon, user=None, status='redeemed', discount=1000, shipping=0, order=None, expires_in=30):
        order = order or self.order(user)
        return CouponRedemption.objects.create(
            coupon=coupon, user=user or self.user, order_id=order.pk, code=coupon.code, status=status, discount_amount=discount,
            shipping_discount=shipping, expires_at=timezone.now() + timedelta(minutes=expires_in),
            redeemed_at=timezone.now() if status == 'redeemed' else None)


# ======================================================================== وضعیت هر کد
class CouponStateTests(WalletBase):
    def state(self, coupon, total=0, mine=0, now=None):
        return wallet.coupon_state(coupon, total, mine, now or timezone.now())

    def test_each_state(self):
        self.assertEqual(self.state(make_coupon('S1')), 'active')
        self.assertEqual(self.state(make_coupon('S2', active=False)), 'inactive')
        self.assertEqual(self.state(make_coupon('S3', expired=True)), 'expired')
        self.assertEqual(self.state(make_coupon('S4', scheduled=True)), 'scheduled')
        self.assertEqual(self.state(make_coupon('S5', total_limit=3), total=3), 'exhausted')
        self.assertEqual(self.state(make_coupon('S6', per_user_limit=1), mine=1), 'used_up')

    def test_precedence_and_boundaries(self):
        both = make_coupon('P1', expired=True, active=False, total_limit=1)
        self.assertEqual(self.state(both, total=5), 'inactive')                                   # غیرفعال از همه بالاتر
        self.assertEqual(self.state(make_coupon('P2', expired=True, total_limit=1), total=5), 'expired')
        self.assertEqual(self.state(make_coupon('P3', scheduled=True, total_limit=1), total=5), 'scheduled')
        self.assertEqual(self.state(make_coupon('P4', total_limit=2, per_user_limit=1), total=1, mine=0), 'active')
        self.assertEqual(self.state(make_coupon('P5', total_limit=2, per_user_limit=1), total=2, mine=1), 'exhausted')
        now = timezone.now()
        edge = make_coupon('P6', starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=1))
        self.assertEqual(self.state(edge, now=edge.ends_at), 'active')
        self.assertEqual(self.state(edge, now=edge.ends_at + timedelta(microseconds=1)), 'expired')

    def test_unlimited_never_runs_out(self):
        self.assertEqual(self.state(make_coupon('U1', total_limit=None, per_user_limit=None), total=10 ** 6, mine=10 ** 6), 'active')


# ======================================================================== قوانین
class RulesTests(WalletBase):
    def rules(self, coupon):
        return ' | '.join(wallet.coupon_rules(coupon))

    def test_percent_with_cap_and_default_no_combine_line(self):
        text = self.rules(make_coupon('R1', value=20, max_discount_amount=50000))
        self.assertIn('تخفیف 20٪', text)
        self.assertIn('حداکثر 50,000 تومان', text)
        self.assertIn('روی همه‌ی کالاهای سبد', text)
        self.assertIn('روی کالاهای دارای تخفیف خودکار اعمال نمی‌شود', text)

    def test_combining_coupon_omits_the_no_combine_line(self):
        self.assertNotIn('اعمال نمی‌شود', self.rules(make_coupon('R2', allow_with_promotions=True)))

    def test_fixed_free_shipping_and_scope(self):
        self.assertIn('15,000 تومان تخفیف', self.rules(make_coupon('R3', kind='fixed', value=15000)))
        text = self.rules(make_coupon('R4', kind='free_shipping'))
        self.assertIn('رایگان می‌شود', text)
        self.assertNotIn('همه‌ی کالاهای سبد', text)
        product = self.product('قلم طلایی')
        scoped = self.rules(make_coupon('R5', products=[product], categories=[self.category]))
        self.assertIn('قلم طلایی', scoped)
        self.assertIn('کیف کد', scoped)

    def test_conditions_window_limits_and_free_text(self):
        now = timezone.now()
        coupon = make_coupon('R6', min_cart_amount=300000, starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=5),
                             per_user_limit=2, first_order_only=True, terms='بند اول\n\n  بند دوم  ')
        text = self.rules(coupon)
        self.assertIn('حداقل مبلغ سبد: 300,000 تومان', text)
        self.assertIn('اعتبار: از', text)
        self.assertIn('حداکثر 2 بار', text)
        self.assertIn('فقط برای اولین خرید', text)
        self.assertEqual(wallet.coupon_rules(coupon)[-2:], ['بند اول', 'بند دوم'])
        self.assertIn('فقط یک بار', self.rules(make_coupon('R7', per_user_limit=1)))
        self.assertIn('اعتبار تا', self.rules(make_coupon('R8', ends_at=now + timedelta(days=1))))
        self.assertIn('اعتبار از', self.rules(make_coupon('R9', starts_at=now - timedelta(days=1))))

    def test_loyalty_line_and_badges(self):
        silver = make_coupon('R10', min_loyalty_level=2)
        self.assertIn('ویژه‌ی', self.rules(silver))
        self.assertEqual(wallet.audience_badge(make_coupon('B1')), 'همگانی')
        self.assertEqual(wallet.audience_badge(make_coupon('B2', audience='assigned')), 'اختصاصی شما')
        self.assertIn('و بالاتر', wallet.audience_badge(silver))


# ======================================================================== کدهای من (سرویس)
class MyCodesServiceTests(WalletBase):
    def codes(self, user=None):
        return wallet.my_codes(user or self.user)

    def test_empty_wallet(self):
        result = self.codes()
        self.assertEqual((result['active'], result['expired'], result['used']), ([], [], []))

    def test_tabs_classification(self):
        live = self.give(make_coupon('LIVE'))
        soon = self.give(make_coupon('SOON', scheduled=True))
        old = self.give(make_coupon('OLD', expired=True))
        off = self.give(make_coupon('OFF', active=False))
        full = self.give(make_coupon('FULL', total_limit=1, per_user_limit=None))
        self.redemption(full.coupon, self.other)                                                   # کس دیگری ظرفیت را برد
        used_up = self.give(make_coupon('USED1', per_user_limit=1))
        self.redemption(used_up.coupon, self.user)
        result = self.codes()
        self.assertEqual({c.code for c in result['active']}, {'LIVE', 'SOON'})
        self.assertEqual({c.code: c.state for c in result['expired']},
                         {'OLD': 'expired', 'OFF': 'inactive', 'FULL': 'exhausted', 'USED1': 'used_up'})
        self.assertEqual({c.code: c.state_label for c in result['expired']}['USED1'], 'استفاده شد')
        self.assertTrue(all(c.is_live for c in result['active']))

    def test_wallet_entries_carry_display_data(self):
        coupon = make_coupon('DATA', value=25, max_discount_amount=40000, min_cart_amount=200000, title='عیدی')
        self.give(coupon, source='claimed')
        item = self.codes()['active'][0]
        self.assertEqual((item.code, item.title, item.value_display, item.min_cart_amount, item.source),
                         ('DATA', 'عیدی', '25٪', 200000, 'claimed'))
        self.assertIn('from-primary', item.gradient)
        self.assertIn('from-success', wallet.WalletCode.gradient.fget(type('X', (), {'kind': 'fixed'})()))
        self.assertTrue(item.rules)

    def test_only_my_assignments_are_listed(self):
        self.give(make_coupon('MINE1'))
        self.give(make_coupon('THEIRS1'), user=self.other)
        self.assertEqual([c.code for c in self.codes()['active']], ['MINE1'])
        self.assertEqual([c.code for c in self.codes(self.other)['active']], ['THEIRS1'])

    def test_newest_assignment_first(self):
        first = self.give(make_coupon('AAA'))
        second = self.give(make_coupon('BBB'))
        UserCoupon.objects.filter(pk=first.pk).update(created_at=timezone.now() - timedelta(days=3))
        self.assertEqual([c.code for c in self.codes()['active']], ['BBB', 'AAA'])
        self.assertIsNotNone(second)

    def test_used_tab_shows_reserved_and_redeemed_but_not_released(self):
        coupon = make_coupon('HIST', per_user_limit=None)
        paid = self.redemption(coupon, discount=45000, shipping=0)
        waiting = self.redemption(coupon, status='reserved', discount=12000)
        self.redemption(coupon, status='released', discount=999)
        free_ship = self.redemption(make_coupon('SHIPUSED', kind='free_shipping', per_user_limit=None), discount=0, shipping=50000)
        rows = self.codes()['used']
        by_order = {r.order_id: r for r in rows}
        self.assertEqual(len(rows), 3)
        self.assertEqual((by_order[paid.order_id].saving, by_order[paid.order_id].status_label), (45000, 'مصرف‌شده'))
        self.assertEqual(by_order[waiting.order_id].status, 'reserved')
        self.assertEqual(by_order[free_ship.order_id].saving, 50000)                                # تخفیف ارسال هم صرفه‌جویی است
        self.assertEqual(by_order[paid.order_id].order_total, 250000)
        self.assertTrue(all(r.has_order for r in rows))

    def test_used_rows_never_include_other_users_and_order_lookup_is_owner_scoped(self):
        coupon = make_coupon('ISO', per_user_limit=None)
        self.redemption(coupon, self.other, discount=7777)
        mine = self.redemption(coupon, discount=1000)
        self.assertEqual([r.order_id for r in self.codes()['used']], [mine.order_id])
        # ردیفِ مصرفِ من که به سفارشِ *دیگری* اشاره کند، لینک/مبلغ آن سفارش را افشا نمی‌کند
        foreign_order = self.order(self.other, total=987654)
        CouponRedemption.objects.filter(pk=mine.pk).update(order_id=foreign_order.pk)
        row = self.codes()['used'][0]
        self.assertEqual((row.has_order, row.order_total), (False, None))

    def test_deleted_order_is_shown_without_a_link(self):
        coupon = make_coupon('GONE', per_user_limit=None)
        redemption = self.redemption(coupon)
        Order.objects.filter(pk=redemption.order_id).delete()
        row = self.codes()['used'][0]
        self.assertFalse(row.has_order)
        self.assertEqual(row.code, 'GONE')

    def test_query_count_is_constant_even_with_scoped_coupons(self):
        product = self.product('محدود')
        for i in range(4):
            self.give(make_coupon(f'BULK{i}', per_user_limit=None))
        for i in range(4):
            self.give(make_coupon(f'SCOPED{i}', per_user_limit=None, products=[product], categories=[self.category]))
        # UserCoupon+coupon، محصولات و دسته‌های prefetch‌شده، شمارش مصرف کل، شمارش مصرف من، مصرف‌های من ← ۶ کوئریِ ثابت
        with self.assertNumQueries(6):
            result = wallet.my_codes(self.user)
        self.assertEqual(len(result['active']), 8)
        self.assertTrue(all(c.rules for c in result['active']))


# ======================================================================== قابل‌دریافت‌ها و دریافت
class ClaimableListTests(WalletBase):
    def cards(self, user=None):
        return {card.pk: card for card in wallet.claimable_coupons(user or self.user)}

    def test_only_claimable_active_public_in_window_coupons(self):
        good = self.claimable('GOOD')
        not_claimable = make_coupon('NOTCLAIM')
        inactive = self.claimable('OFF', active=False)
        expired = self.claimable('OLD', expired=True)
        scheduled = self.claimable('SOON', scheduled=True)
        assigned = self.claimable('PRIV')
        Coupon.objects.filter(pk=assigned.pk).update(audience='assigned')                     # DB اجازه می‌دهد؛ صفحه نباید نشان دهد
        cards = self.cards()
        self.assertEqual(set(cards), {good.pk})
        for hidden in (not_claimable, inactive, expired, scheduled, assigned):
            self.assertNotIn(hidden.pk, cards)

    def test_owned_coupons_are_not_listed_again(self):
        coupon = self.claimable('OWN')
        self.give(coupon, source='claimed')
        self.assertEqual(self.cards(), {})
        self.assertIn(coupon.pk, self.cards(self.other))

    def test_capacity_hides_full_coupons(self):
        claim_full = self.claimable('CF', claim_limit=1)
        self.give(claim_full, self.other, source='claimed')
        use_full = self.claimable('UF', total_limit=1, per_user_limit=None)
        self.redemption(use_full, self.other)
        open_one = self.claimable('OPEN', claim_limit=5)
        cards = self.cards()
        self.assertEqual(set(cards), {open_one.pk})
        self.assertEqual(cards[open_one.pk].remaining, 5)

    def test_remaining_counts_down(self):
        coupon = self.claimable('CNT', claim_limit=3)
        self.give(coupon, self.other, source='claimed')
        unlimited = self.claimable('UNL')
        cards = self.cards()
        self.assertEqual(cards[coupon.pk].remaining, 2)
        self.assertIsNone(cards[unlimited.pk].remaining)

    def test_first_order_only_is_hidden_after_an_order(self):
        coupon = self.claimable('FIRST', first_order_only=True)
        self.assertIn(coupon.pk, self.cards())
        self.order()
        self.assertNotIn(coupon.pk, self.cards())

    def test_first_order_only_fails_closed_when_the_stat_is_unavailable(self):
        from unittest import mock
        coupon = self.claimable('FIRSTX', first_order_only=True)
        with mock.patch('promotions.wallet.get_stat', return_value=None):
            self.assertNotIn(coupon.pk, self.cards())

    def test_loyalty_level_gate(self):
        coupon = self.claimable('SILVER', min_loyalty_level=2)                               # «نقره‌ای» = ۷ سفارش پرداخت‌شده
        self.assertNotIn(coupon.pk, self.cards())
        veteran = self.new_user()
        veteran.paid_orders_count = 9
        self.assertIn(coupon.pk, self.cards(veteran))

    def test_cards_never_expose_the_code(self):
        self.claimable('SECRETCODE', title='عنوان بدون کد')
        card = next(iter(self.cards().values()))
        self.assertFalse(hasattr(card, 'code'))
        self.assertNotIn('SECRETCODE', repr(card))

    def test_newest_first(self):
        first = self.claimable('N1')
        second = self.claimable('N2')
        Coupon.objects.filter(pk=first.pk).update(created_at=timezone.now() - timedelta(days=2))
        self.assertEqual([c.pk for c in wallet.claimable_coupons(self.user)], [second.pk, first.pk])


class ClaimServiceTests(WalletBase):
    def test_successful_claim(self):
        coupon = self.claimable('WIN')
        result = wallet.claim(self.user, coupon.pk)
        self.assertEqual((result.status, result.ok, result.code), ('claimed', True, 'WIN'))
        self.assertTrue(result.rules)
        row = UserCoupon.objects.get()
        self.assertEqual((row.user, row.coupon, row.source), (self.user, coupon, 'claimed'))
        self.assertEqual([c.code for c in wallet.my_codes(self.user)['active']], ['WIN'])
        self.assertEqual(wallet.claimable_coupons(self.user), [])

    def test_claiming_twice_is_idempotent(self):
        coupon = self.claimable('TWICE')
        wallet.claim(self.user, coupon.pk)
        again = wallet.claim(self.user, coupon.pk)
        self.assertEqual((again.status, again.ok, again.code), ('already', True, 'TWICE'))
        self.assertEqual(UserCoupon.objects.count(), 1)

    def test_claim_limit_is_enforced_per_coupon(self):
        coupon = self.claimable('LIM', claim_limit=2)
        users = [self.new_user() for _ in range(4)]
        results = [wallet.claim(u, coupon.pk).status for u in users]
        self.assertEqual(results, ['claimed', 'claimed', 'full', 'full'])
        self.assertEqual(UserCoupon.objects.filter(coupon=coupon).count(), 2)

    def test_a_holder_can_still_see_their_own_coupon_when_it_is_full(self):
        coupon = self.claimable('HOLD', claim_limit=1)
        wallet.claim(self.user, coupon.pk)
        again = wallet.claim(self.user, coupon.pk)
        self.assertEqual(again.status, 'already')
        self.assertEqual(wallet.claim(self.other, coupon.pk).status, 'full')

    def test_total_usage_cap_blocks_new_claims(self):
        coupon = self.claimable('USECAP', total_limit=1, per_user_limit=None)
        self.redemption(coupon, self.other)
        self.assertEqual(wallet.claim(self.user, coupon.pk).status, 'full')

    def test_every_unavailable_case_gets_the_same_answer_and_no_code(self):
        good = self.claimable('GOOD1')
        cases = {
            'nonexistent': 987654321,
            'not_claimable': make_coupon('NC').pk,
            'inactive': self.claimable('IN', active=False).pk,
            'expired': self.claimable('EX', expired=True).pk,
            'scheduled': self.claimable('SC', scheduled=True).pk,
            'first_order_taken': self.claimable('FO', first_order_only=True).pk,
            'loyalty': self.claimable('LO', min_loyalty_level=3).pk,
        }
        private = self.claimable('PRIV')
        Coupon.objects.filter(pk=private.pk).update(audience='assigned')
        self.order()                                                                        # «اولین خرید» را می‌سوزاند
        cases['assigned_private'] = private.pk
        answers = {name: wallet.claim(self.user, pk) for name, pk in cases.items()}
        for name, result in answers.items():
            with self.subTest(case=name):
                self.assertEqual((result.status, result.message, result.code, result.rules, result.coupon),
                                 ('unavailable', wallet.CLAIM_MESSAGES['unavailable'], '', (), None))
        self.assertEqual(UserCoupon.objects.count(), 0)
        self.assertEqual(wallet.claim(self.user, good.pk).status, 'claimed')

    def test_private_coupon_of_another_user_cannot_be_claimed_by_guessing(self):
        private = make_coupon('ONLYHIS', audience='assigned')
        self.give(private, self.other)
        result = wallet.claim(self.user, private.pk)
        self.assertEqual(result.status, 'unavailable')
        self.assertFalse(UserCoupon.objects.filter(user=self.user).exists())
        self.assertNotIn('ONLYHIS', repr(result))

    def test_claim_uses_server_time(self):
        coupon = self.claimable('TIME', ends_at=timezone.now() + timedelta(hours=1))
        self.assertEqual(wallet.claim(self.user, coupon.pk, now=coupon.ends_at + timedelta(seconds=1)).status, 'unavailable')
        self.assertEqual(wallet.claim(self.user, coupon.pk, now=coupon.ends_at).status, 'claimed')

    def test_claim_needs_the_row_lock_inside_a_transaction(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        coupon = self.claimable('LOCK')
        with CaptureQueriesContext(connection) as queries:
            wallet.claim(self.user, coupon.pk)
        self.assertTrue(any('UPDLOCK' in q['sql'].upper() and 'PROMOTIONS_COUPON' in q['sql'].upper() for q in queries.captured_queries))

    def test_database_unique_constraint_backs_the_lock(self):
        from django.db import IntegrityError, transaction
        coupon = self.claimable('UNIQ')
        self.give(coupon)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.give(coupon)


class CouponModelClaimFieldTests(WalletBase):
    def test_claimable_requires_the_public_audience(self):
        from django.core.exceptions import ValidationError
        coupon = Coupon(code='X1Y2', title='t', kind='percent', value=10, is_claimable=True, audience='assigned')
        with self.assertRaises(ValidationError) as ctx:
            coupon.full_clean(exclude=['products', 'categories'])
        self.assertIn('is_claimable', ctx.exception.message_dict)

    def test_claim_limit_must_be_positive(self):
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError) as ctx:
            Coupon(code='X1Y3', title='t', kind='percent', value=10, claim_limit=0).full_clean(exclude=['products', 'categories'])
        self.assertIn('claim_limit', ctx.exception.message_dict)

    def test_loyalty_level_also_gates_usage_at_checkout(self):
        cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(cart=cart, product=self.product(), quantity=1)
        pricing = price_cart(list(cart.items.select_related('product')), self.user, CHECK)
        coupon = make_coupon('GATE', min_loyalty_level=2)
        result = coupons.evaluate_coupon(coupon, self.user, pricing)
        self.assertEqual((result.ok, result.error), (False, coupons.NOT_ELIGIBLE))
        self.assertFalse(result.is_guess_failure)
        veteran = self.new_user()
        veteran.paid_orders_count = 8
        self.assertTrue(coupons.evaluate_coupon(coupon, veteran, price_cart(list(cart.items.select_related('product')), veteran, CHECK)).ok)

    def test_a_claimed_code_works_at_checkout(self):
        coupon = self.claimable('USEIT', value=10, min_cart_amount=50000)
        wallet.claim(self.user, coupon.pk)
        cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(cart=cart, product=self.product(price=200000), quantity=1)
        pricing = price_cart(list(cart.items.select_related('product')), self.user, CHECK)
        result = coupons.evaluate_coupon(coupons.find_coupon('useit'), self.user, pricing)
        self.assertEqual((result.ok, result.item_discount), (True, Decimal('20000')))


# ======================================================================== صفحه‌ها
class PanelPagesAccessTests(WalletBase):
    def test_login_is_required_everywhere(self):
        self.client.logout()
        coupon = self.claimable('LOGIN')
        for url in (reverse(MY), reverse(GET)):
            self.assertEqual(self.client.get(url).status_code, 302, url)
        self.assertEqual(self.client.post(reverse(CLAIM, args=[coupon.pk])).status_code, 302)
        self.assertFalse(UserCoupon.objects.exists())

    def test_claim_is_post_only(self):
        coupon = self.claimable('POSTONLY')
        self.assertEqual(self.client.get(reverse(CLAIM, args=[coupon.pk])).status_code, 405)
        self.assertFalse(UserCoupon.objects.exists())

    def test_the_old_coming_soon_placeholder_is_gone(self):
        from django.urls import NoReverseMatch
        with self.assertRaises(NoReverseMatch):
            reverse('accounts:soon_discounts')


class MyCodesPageTests(WalletBase):
    def html(self, **params):
        response = self.client.get(reverse(MY), params)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_empty_states_and_counts(self):
        html = self.html()
        self.assertIn('کد تخفیف فعالی ندارید', html)
        self.assertIn('کد تخفیف استفاده‌شده‌ای ندارید', html)
        self.assertIn('کد منقضی‌شده‌ای ندارید', html)
        self.assertIn('(0)', html)
        self.assertIn(reverse(GET), html)

    def test_three_tabs_with_the_right_content(self):
        live = self.give(make_coupon('LIVE1', value=30, title='تخفیف زنده', min_cart_amount=500000))
        old = self.give(make_coupon('OLD1', expired=True, title='تخفیف قدیمی'))
        used = make_coupon('USED2', per_user_limit=None, title='استفاده شد')
        redemption = self.redemption(used, discount=45000)
        self.give(used)
        html = self.html()
        for tab in ('active-tab', 'used-tab', 'expired-tab'):
            self.assertIn(f'id="{tab}"', html)
        active_part = html[html.index('id="active" role="tabpanel"'):html.index('id="used" role="tabpanel"')]
        used_part = html[html.index('id="used" role="tabpanel"'):html.index('id="expired" role="tabpanel"')]
        expired_part = html[html.index('id="expired" role="tabpanel"'):]
        self.assertIn('LIVE1', active_part)
        self.assertIn('30٪', active_part)
        self.assertIn('500000 تومان', active_part)
        self.assertIn('data-copy="LIVE1"', active_part)
        self.assertNotIn('OLD1', active_part)
        self.assertIn('OLD1', expired_part)
        self.assertIn('منقضی شده', expired_part)
        self.assertNotIn('data-copy="OLD1"', expired_part)                                # کد منقضی دکمه‌ی کپی ندارد
        self.assertIn('USED2', used_part)
        self.assertIn(reverse('orders:order_detail_full', args=[redemption.order_id]), used_part)
        self.assertIn('45000 تومان', used_part)
        self.assertIn('250000 تومان', used_part)                                           # مبلغ فاکتور
        self.assertIn('پرداخت‌شده', used_part)
        self.assertIsNotNone(live)
        self.assertIsNotNone(old)

    def test_rules_modal_content_per_code(self):
        self.give(make_coupon('RULES1', value=15, min_cart_amount=120000, terms='فقط برای خرید آنلاین'))
        html = self.html()
        self.assertIn('data-rules-open="rules-1-a"', html)
        block = html[html.index('id="rules-1-a"'):]
        block = block[:block.index('</template>')]
        self.assertIn('حداقل مبلغ سبد: 120,000 تومان', block)
        self.assertIn('فقط برای خرید آنلاین', block)
        self.assertIn('id="rules-general"', html)
        self.assertIn('id="rules-modal"', html)

    def test_scheduled_code_shows_its_start(self):
        self.give(make_coupon('LATER', scheduled=True))
        self.assertIn('فعال می‌شود', self.html())

    def test_tab_query_activates_the_tab(self):
        html = self.html(tab='used')
        self.assertRegex(html, r'id="used-tab"[^>]*aria-selected="true"')
        self.assertIn('class="p-4 rounded-lg bg-white dark:bg-gray-800 shadow-soft dark:shadow-soft-dark border border-gray-100 dark:border-gray-700" id="used"', html)
        self.assertRegex(self.html(tab='bogus'), r'id="active-tab"[^>]*aria-selected="true"')

    def test_other_users_private_codes_never_appear(self):
        self.give(make_coupon('HIS-SECRET-1', audience='assigned', title='ویژه‌ی او'), self.other)
        theirs_used = make_coupon('HIS-USED-2', per_user_limit=None)
        self.redemption(theirs_used, self.other, discount=88888)
        self.claimable('PUBLICCLAIM')                                                       # کد قابل‌دریافتِ دریافت‌نشده هم نباید متنش بیاید
        html = self.html()
        for secret in ('HIS-SECRET-1', 'HIS-USED-2', '88888', 'ویژه‌ی او', 'PUBLICCLAIM'):
            self.assertNotIn(secret, html, secret)

    def test_panel_navigation_links_to_the_page(self):
        html = self.html()
        self.assertIn(f'href="{reverse(MY)}"', html)
        self.assertIn('کدهای تخفیف من', html)
        self.assertNotIn('تخفیف‌ها و کارت هدیه', html)

    def test_query_count_does_not_grow_with_the_number_of_codes(self):
        for i in range(3):
            self.give(make_coupon(f'Q1{i}', per_user_limit=None))
        self.client.get(reverse(MY))
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as few:
            self.client.get(reverse(MY))
        for i in range(12):
            self.give(make_coupon(f'Q2{i}', per_user_limit=None))
        with CaptureQueriesContext(connection) as many:
            self.client.get(reverse(MY))
        self.assertEqual(len(few), len(many))


class GetCodePageTests(WalletBase):
    def test_lists_cards_without_the_code(self):
        self.claimable('HIDDENCODE', value=25, title='عیدی ویژه', min_cart_amount=300000, claim_limit=10)
        response = self.client.get(reverse(GET))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('عیدی ویژه', html)
        self.assertIn('25٪', html)
        self.assertIn('300000 تومان', html)
        self.assertIn('10 ظرفیت دریافت باقی مانده', html)
        self.assertIn('دریافت کد', html)
        self.assertNotIn('HIDDENCODE', html)                                                # متن کد تا قبل از دریافت دیده نمی‌شود
        self.assertIn('id="claim-modal"', html)

    def test_empty_state(self):
        self.assertContains(self.client.get(reverse(GET)), 'کد تخفیفی برای دریافت وجود ندارد')

    def test_ineligible_coupons_are_not_listed(self):
        self.claimable('FIRSTONLY', first_order_only=True, title='فقط اولین خرید')
        self.order()
        self.claimable('LEVEL', min_loyalty_level=3, title='فقط ویژه‌ها')
        html = self.client.get(reverse(GET)).content.decode()
        self.assertNotIn('فقط اولین خرید', html.replace('راهنما', ''))
        self.assertNotIn('فقط ویژه‌ها', html)

    def test_private_and_foreign_codes_are_absent(self):
        private = make_coupon('PRIVATE-X', audience='assigned', title='اختصاصی X')
        self.give(private, self.other)
        self.assertNotIn('PRIVATE-X', self.client.get(reverse(GET)).content.decode())
        self.assertNotIn('اختصاصی X', self.client.get(reverse(GET)).content.decode())


class ClaimViewTests(WalletBase):
    def post(self, pk, htmx=False, user=None):
        extra = {'HTTP_HX_REQUEST': 'true'} if htmx else {}
        return self.client.post(reverse(CLAIM, args=[pk]), **extra)

    def test_plain_post_redirects_to_my_codes_with_a_message(self):
        coupon = self.claimable('PLAIN')
        response = self.post(coupon.pk)
        self.assertRedirects(response, reverse(MY), fetch_redirect_response=False)
        page = self.client.get(reverse(MY))
        self.assertContains(page, 'کد تخفیف به «کدهای من» شما اضافه شد.')
        self.assertContains(page, 'data-copy="PLAIN"')

    def test_plain_post_failure_redirects_back_with_the_generic_message(self):
        response = self.post(424242)
        self.assertRedirects(response, reverse(GET), fetch_redirect_response=False)
        self.assertContains(self.client.get(reverse(GET)), wallet.CLAIM_MESSAGES['unavailable'])

    def test_htmx_success_returns_the_code_modal_and_updates_the_card(self):
        coupon = self.claimable('HTMXOK')
        response = self.post(coupon.pk, htmx=True)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('id="claimed-code"', html)
        self.assertIn('HTMXOK', html)
        self.assertIn('data-copy="HTMXOK"', html)
        self.assertIn(f'id="claim-card-{coupon.pk}" hx-swap-oob="true"', html)
        self.assertIn('دریافت شد', html)
        self.assertEqual(UserCoupon.objects.filter(user=self.user, coupon=coupon).count(), 1)

    def test_htmx_second_click_reports_already_without_creating_a_second_row(self):
        coupon = self.claimable('DBL')
        self.post(coupon.pk, htmx=True)
        html = self.post(coupon.pk, htmx=True).content.decode()
        self.assertIn('قبلاً برای شما دریافت شده است', html)
        self.assertEqual(UserCoupon.objects.filter(coupon=coupon).count(), 1)

    def test_full_coupon(self):
        coupon = self.claimable('FULL1', claim_limit=1)
        self.give(coupon, self.other, source='claimed')
        html = self.post(coupon.pk, htmx=True).content.decode()
        self.assertIn(wallet.CLAIM_MESSAGES['full'], html)
        self.assertNotIn('FULL1', html)
        self.assertFalse(UserCoupon.objects.filter(user=self.user).exists())

    def test_guessing_ids_never_reveals_anyone_elses_private_code(self):
        secrets = []
        for i in range(6):
            coupon = make_coupon(f'PRIV-SECRET-{i}', audience='assigned', title=f'اختصاصی {i}')
            self.give(coupon, self.other)
            secrets.append(coupon)
        baseline = self.post(10 ** 9, htmx=True).content.decode()
        for coupon in secrets:
            html = self.post(coupon.pk, htmx=True).content.decode()
            self.assertNotIn(coupon.code, html)
            self.assertNotIn(f'اختصاصی', html)
            # بجز شناسه‌ی کارت، پاسخ با پاسخ به شناسه‌ی ناموجود یکسان است
            self.assertEqual(html.replace(f'claim-card-{coupon.pk}', 'claim-card-X'), baseline.replace(f'claim-card-{10 ** 9}', 'claim-card-X'))
        self.assertFalse(UserCoupon.objects.filter(user=self.user).exists())

    def test_guessing_ids_of_claimable_coupons_only_yields_what_the_page_would_offer(self):
        hidden_for_me = self.claimable('LVL', min_loyalty_level=3)
        html = self.post(hidden_for_me.pk, htmx=True).content.decode()
        self.assertNotIn('LVL', html)
        self.assertIn(wallet.CLAIM_MESSAGES['unavailable'], html)

    def test_claim_is_bound_to_the_logged_in_user_only(self):
        coupon = self.claimable('BOUND')
        self.client.post(reverse(CLAIM, args=[coupon.pk]), {'user': self.other.pk, 'user_id': self.other.pk})
        self.assertEqual(list(UserCoupon.objects.values_list('user_id', flat=True)), [self.user.pk])

    def test_redemptions_of_a_claimed_code_appear_in_the_used_tab(self):
        coupon = self.claimable('FLOW', per_user_limit=None)
        self.post(coupon.pk)
        self.redemption(coupon, discount=9000)
        html = self.client.get(reverse(MY)).content.decode()
        used_part = html[html.index('id="used" role="tabpanel"'):html.index('id="expired" role="tabpanel"')]
        self.assertIn('FLOW', used_part)
        self.assertIn('9000 تومان', used_part)


class PanelAdminTests(WalletBase):
    def setUp(self):
        super().setUp()
        self.admin_user = CustomUser.objects.create_superuser(phone_number=f'0912015{next(_seq):04d}')
        self.client.force_login(self.admin_user)

    def test_coupon_form_has_the_claim_fields_and_saves_them(self):
        page = self.client.get(reverse('admin:promotions_coupon_add'))
        for name in ('is_claimable', 'claim_limit', 'terms', 'min_loyalty_level'):
            self.assertContains(page, f'name="{name}"')

    def test_change_page_reports_how_many_claimed(self):
        coupon = self.claimable('ADMINCLAIM')
        for _ in range(3):
            self.give(coupon, self.new_user(), source='claimed')
        page = self.client.get(reverse('admin:promotions_coupon_change', args=[coupon.pk]))
        self.assertContains(page, 'دریافت‌شده در پنل: <b>3</b>')

    def test_list_filter_for_claimable(self):
        self.claimable('CL1')
        make_coupon('NCL1')
        body = self.client.get(reverse('admin:promotions_coupon_changelist'), {'is_claimable__exact': '1'}).content.decode()
        self.assertIn('CL1', body)
        self.assertNotIn('NCL1', body)

    def test_user_inline_shows_source_and_lets_the_admin_assign(self):
        coupon = make_coupon('ASSIGN1', audience='assigned')
        page = self.client.get(reverse('admin:promotions_coupon_change', args=[coupon.pk]))
        self.assertContains(page, 'assignments-TOTAL_FORMS')


# ======================================================================== نشت متن کد در عنوان/قوانین
class CodeLeakValidationTests(WalletBase):
    """ کدِ قابل‌دریافت تا قبل از دریافت دیده نمی‌شود؛ تکرار متن کد در عنوان/قوانین ذخیره نمی‌شود """

    def coupon(self, code='YALDA1405', **fields):
        data = dict(code=code, title='عیدی یلدا', kind='percent', value=10, is_claimable=True)
        data.update(fields)
        return Coupon(**data)

    def errors(self, coupon):
        from django.core.exceptions import ValidationError
        try:
            coupon.full_clean(exclude=['products', 'categories'])
        except ValidationError as error:
            return error.message_dict
        return {}

    def test_clean_title_passes(self):
        self.assertEqual(self.errors(self.coupon()), {})

    def test_exact_and_case_and_space_variants_in_the_title_are_rejected(self):
        variants = ('کد YALDA1405 را بزنید', 'yalda1405', 'Yalda1405', 'y a l d a 1 4 0 5', 'YALDA‌1405', 'کد: yalda 1405 !')
        for title in variants:
            with self.subTest(title=title):
                errors = self.errors(self.coupon(title=title))
                self.assertIn('title', errors)
                self.assertIn('متنِ خودِ کد', errors['title'][0])

    def test_persian_digits_dashes_and_underscores_are_seen_through(self):
        self.assertIn('title', self.errors(self.coupon(title='کد yalda۱۴۰۵')))
        self.assertIn('title', self.errors(self.coupon(code='YALDA-1405', title='کد yalda1405')))
        self.assertIn('title', self.errors(self.coupon(code='YALDA1405', title='کد yalda-1405')))
        self.assertIn('title', self.errors(self.coupon(code='NEW_YEAR', title='کد new year')))
        self.assertIn('title', self.errors(self.coupon(code='NEW-YEAR', title='کد new_year')))

    def test_the_terms_shown_to_users_are_checked_too(self):
        errors = self.errors(self.coupon(terms='بند اول\nاین کد: YALDA1405 است'))
        self.assertIn('terms', errors)
        self.assertNotIn('title', errors)

    def test_both_fields_are_reported_together(self):
        errors = self.errors(self.coupon(title='YALDA1405', terms='yalda1405'))
        self.assertEqual({'title', 'terms'}, set(errors) & {'title', 'terms'})

    def test_non_claimable_coupons_may_mention_their_code(self):
        self.assertEqual(self.errors(self.coupon(is_claimable=False, title='کد YALDA1405 برای اینستاگرام')), {})

    def test_the_internal_description_is_not_checked(self):
        self.assertEqual(self.errors(self.coupon(description='این کد YALDA1405 برای کمپین یلدا است')), {})

    def test_unrelated_and_partial_text_is_fine(self):
        self.assertEqual(self.errors(self.coupon(title='تخفیف یلدا 1405')), {})               # فقط بخشی از کد
        self.assertEqual(self.errors(self.coupon(title='YALDA و 1405 جدا')), {})              # کلمه‌ها جدا و با متن میانی
        self.assertEqual(self.errors(self.coupon(terms='شب یلدا')), {})

    def test_a_blank_code_cannot_leak_and_is_not_checked(self):
        self.assertEqual(self.errors(self.coupon(code='', title='هر متنی')), {})

    def test_leaking_fields_api(self):
        self.assertEqual(self.coupon(title='yalda1405', terms='x').leaking_fields(), ['title'])
        self.assertEqual(self.coupon().leaking_fields(), [])
        self.assertEqual(self.coupon(code='AB', title='ab').leaking_fields(), [])              # کوتاه‌تر از ۳ نویسه بررسی نمی‌شود

    def test_editing_an_existing_coupon_into_claimable_is_checked_too(self):
        stored = make_coupon('WINTER25', title='کد WINTER25 برای زمستان')                       # عمومی؛ مجاز
        stored.is_claimable = True
        self.assertIn('title', self.errors(stored))

    # ---- ادمین ----
    def admin_data(self, **overrides):
        data = {
            'code': 'SECRET77', 'title': 'عیدی ویژه', 'description': '', 'is_active': 'on', 'kind': 'percent', 'value': '10',
            'max_discount_amount': '', 'scope': 'cart', 'min_cart_amount': '0', 'allow_with_promotions': '', 'total_limit': '',
            'per_user_limit': '1', 'first_order_only': '', 'audience': 'everyone', 'min_loyalty_level': '0', 'is_claimable': 'on',
            'claim_limit': '', 'terms': '',
            'assignments-TOTAL_FORMS': '0', 'assignments-INITIAL_FORMS': '0', 'assignments-MIN_NUM_FORMS': '0', 'assignments-MAX_NUM_FORMS': '1000',
            'redemptions-TOTAL_FORMS': '0', 'redemptions-INITIAL_FORMS': '0', 'redemptions-MIN_NUM_FORMS': '0', 'redemptions-MAX_NUM_FORMS': '0',
        }
        data.update(overrides)
        return data

    def test_admin_refuses_to_save_a_claimable_coupon_that_repeats_its_code(self):
        admin_user = CustomUser.objects.create_superuser(phone_number=f'0912016{next(_seq):04d}')
        self.client.force_login(admin_user)
        url = reverse('admin:promotions_coupon_add')
        response = self.client.post(url, self.admin_data(title='کد secret77 را وارد کنید'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'متنِ خودِ کد در این فیلد تکرار شده است')
        self.assertFalse(Coupon.objects.filter(code='SECRET77').exists())
        response = self.client.post(url, self.admin_data(terms='یادآوری: SECRET-77'))
        self.assertContains(response, 'متنِ خودِ کد در این فیلد تکرار شده است')
        self.assertFalse(Coupon.objects.filter(code='SECRET77').exists())
        self.assertEqual(self.client.post(url, self.admin_data()).status_code, 302)
        self.assertTrue(Coupon.objects.get(code='SECRET77').is_claimable)

    def test_admin_allows_the_same_text_once_the_coupon_is_not_claimable(self):
        admin_user = CustomUser.objects.create_superuser(phone_number=f'0912016{next(_seq):04d}')
        self.client.force_login(admin_user)
        response = self.client.post(reverse('admin:promotions_coupon_add'),
                                    self.admin_data(title='کد SECRET77 برای شبکه‌های اجتماعی', is_claimable=''))
        self.assertEqual(response.status_code, 302)

    # ---- لایه‌ی دفاعی در صفحه‌ی «دریافت» ----
    def test_a_legacy_leaking_coupon_is_never_listed(self):
        good = self.claimable('GOODONE', title='عنوان امن')
        leaky = self.claimable('LEAKYCODE', title='عنوان امن ۲')
        Coupon.objects.filter(pk=leaky.pk).update(title='کد LEAKYCODE را بزنید')                # ذخیره‌ی مستقیم، بدون clean
        with self.assertLogs('promotions.wallet', level='WARNING') as logs:
            cards = {c.pk for c in wallet.claimable_coupons(self.user)}
        self.assertEqual(cards, {good.pk})
        self.assertTrue(any('متن خودش را دارد' in line for line in logs.output))

    def test_the_get_page_never_prints_a_code_even_for_a_legacy_leaking_coupon(self):
        leaky = self.claimable('LEAKPAGE', title='x')
        Coupon.objects.filter(pk=leaky.pk).update(terms='کد LEAKPAGE')
        html = self.client.get(reverse(GET)).content.decode()
        self.assertNotIn('LEAKPAGE', html)
