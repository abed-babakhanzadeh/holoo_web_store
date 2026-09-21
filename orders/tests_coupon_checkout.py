"""
تست کد تخفیف در تسویه‌حساب (اعمال/حذف HTMX، فاکتور زنده، ثبت سفارش، اسنپ‌شات، رزرو/مصرف/لغو) و کنترل تغییر قیمت
(expected_total): مقدار ارسالی فقط مقایسه می‌شود، هر مغایرتِ حتی ۱ ریال ثبت را متوقف و صفحه را با فاکتور تازه و پیام
هشدار دوباره نشان می‌دهد؛ همه‌ی مراحل داخل یک تراکنش اتمیک‌اند.
"""

from datetime import timedelta
from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomUser
from cart.models import Cart, CartItem
from holoo.invoice import build_invoice_payload, item_lines, payload_total
from orders.models import Order
from orders.tests import CheckoutTestBase
from orders.views import PRICE_DRIFT_MESSAGE
from promotions import coupons, ratelimit
from promotions.models import Coupon, CouponRedemption, DiscountPolicy, UserCoupon
from promotions.testing import (
    TEST_CLIENT_IP, make_coupon, make_free_shipping_rule, make_promotion, reset_coupon_attempts, reset_promotions_cache,
)

APPLY = 'orders:apply_coupon'
REMOVE = 'orders:remove_coupon'
INVOICE = 'orders:update_invoice'


class CouponCheckoutBase(CheckoutTestBase):
    """ سبد ۲×۱۰۰٬۰۰۰ (= ۲۰۰٬۰۰۰)، آدرس پستی پیش‌فرض و آدرس پیکیِ قم با تعرفه‌ی ۴۵٬۰۰۰ """

    def setUp(self):
        super().setUp()
        DiscountPolicy.load()
        reset_coupon_attempts(self.user, self.other)
        self.addCleanup(reset_coupon_attempts, self.user, self.other)
        self.courier = self.make_address(self.user, self.zoned_city, self.zone, title='پیکی')

    # ---- کمکی‌ها ----
    def apply(self, code, address=None, method='check'):
        return self.client.post(reverse(APPLY), {'code': code, 'payment_method': method,
                                                 'address_id': (address or self.address).pk})

    def remove(self, address=None, method='check'):
        return self.client.post(reverse(REMOVE), {'payment_method': method, 'address_id': (address or self.address).pk})

    def invoice(self, address=None, method='check'):
        return self.client.get(reverse(INVOICE), {'payment_method': method, 'address_id': (address or self.address).pk})

    def session_code(self):
        return self.client.session.get(coupons.SESSION_KEY)

    def place(self, address=None, method='check', **extra):
        data = {'address_id': (address or self.address).pk, 'payment_method': method, **extra}
        with mock.patch('holoo.receivers.send_order_to_holoo'):
            with self.captureOnCommitCallbacks(execute=True):
                return self.post_order(data)

    def order(self):
        return Order.objects.get(user=self.user)


# ======================================================================== اعمال و حذف
class ApplyRemoveCouponTests(CouponCheckoutBase):
    def test_apply_updates_the_live_invoice_and_remembers_the_code(self):
        make_coupon('SAVE20', value=20)
        response = self.apply('SAVE20')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.session_code(), 'SAVE20')
        self.assertEqual(response.context['final_total'], 160000)
        self.assertEqual(response.context['coupon_discount'], 40000)
        self.assertContains(response, 'کد تخفیف (SAVE20):')
        self.assertContains(response, 'اعمال شد')
        self.assertContains(response, 'حذف')
        self.assertContains(response, 'id="expected-total" value="160000"')

    def test_code_is_case_and_digit_insensitive(self):
        make_coupon('SAVE10', value=10)
        for typed in ('save10', ' Save۱۰ ', 'SAVE١٠'):
            self.remove()
            self.assertEqual(self.apply(typed).status_code, 200)
            self.assertEqual(self.session_code(), 'SAVE10', typed)

    def test_remove_restores_the_original_total(self):
        make_coupon('SAVE20', value=20)
        self.apply('SAVE20')
        response = self.remove()
        self.assertIsNone(self.session_code())
        self.assertEqual(response.context['final_total'], 200000)
        self.assertContains(response, 'کد تخفیف حذف شد.')
        self.assertContains(response, 'id="coupon-code-input"')
        self.assertContains(response, 'id="expected-total" value="200000"')

    def test_applying_a_second_code_replaces_the_first(self):
        make_coupon('A10', value=10)
        make_coupon('B30', value=30)
        self.apply('A10')
        response = self.apply('B30')
        self.assertEqual(self.session_code(), 'B30')
        self.assertEqual(response.context['coupon_discount'], 60000)

    def test_a_failed_attempt_keeps_the_previous_valid_code(self):
        make_coupon('KEEP', value=10)
        self.apply('KEEP')
        response = self.apply('NOPE')
        self.assertEqual(self.session_code(), 'KEEP')
        self.assertContains(response, 'کد تخفیف معتبر نیست.')
        self.assertEqual(response.context['coupon_discount'], 20000)

    def test_invalid_empty_expired_inactive_messages(self):
        make_coupon('OLD', expired=True)
        make_coupon('OFF', active=False)
        cases = (('NOPE', 'کد تخفیف معتبر نیست.'), ('', 'کد تخفیف را وارد کنید.'), ('OLD', 'به پایان رسیده'), ('OFF', 'فعال نیست'))
        for code, message in cases:
            with self.subTest(code=code):
                self.assertContains(self.apply(code), message)
                self.assertIsNone(self.session_code())

    def test_cart_rule_errors_are_specific_and_do_not_store_the_code(self):
        make_coupon('BIG', min_cart_amount=10 ** 7)
        response = self.apply('BIG')
        self.assertContains(response, 'حداقل مبلغ سبد')
        self.assertIsNone(self.session_code())

    def test_combination_rule_error_when_every_item_has_an_automatic_discount(self):
        make_promotion(self.product, percent=10)
        make_coupon('NOCOMBO', value=10)
        response = self.apply('NOCOMBO')
        self.assertContains(response, 'قابل ترکیب نیست')
        self.assertIsNone(self.session_code())

    def test_combination_flag_allows_it(self):
        make_promotion(self.product, percent=10)                      # ۲×۹۰٬۰۰۰ = ۱۸۰٬۰۰۰
        make_coupon('COMBO', value=10, allow_with_promotions=True)
        response = self.apply('COMBO')
        self.assertEqual(self.session_code(), 'COMBO')
        self.assertEqual(response.context['final_total'], 162000)

    def test_partial_combination_applies_to_the_undiscounted_lines_and_says_so(self):
        second = self.product.__class__.objects.create(name='دوم', slug='coupon-second', erp_code='ERP-C-2',
                                                       category=self.product.category, price=50000, price2=45000, stock=9)
        CartItem.objects.create(cart=self.cart, product=second, quantity=2)
        make_promotion(self.product, percent=10)                      # کالای اول تخفیف خودکار دارد؛ دوم ندارد
        make_coupon('PART', value=20)
        response = self.apply('PART')
        self.assertEqual(response.context['coupon_discount'], 20000)   # ۲۰٪ از ۲×۵۰٬۰۰۰ فقط
        self.assertContains(response, 'روی 1 قلم دارای تخفیف خودکار اعمال نمی‌شود')

    def test_login_is_required(self):
        self.client.logout()
        self.assertEqual(self.client.post(reverse(APPLY), {'code': 'X'}).status_code, 302)
        self.assertEqual(self.client.post(reverse(REMOVE)).status_code, 302)

    def test_only_post_is_accepted(self):
        self.assertEqual(self.client.get(reverse(APPLY)).status_code, 405)
        self.assertEqual(self.client.get(reverse(REMOVE)).status_code, 405)

    def test_assigned_only_code_belongs_to_its_users(self):
        coupon = make_coupon('MINE', audience='assigned')
        self.assertContains(self.apply('MINE'), 'برای حساب شما تعریف نشده است')
        UserCoupon.objects.create(coupon=coupon, user=self.user)
        self.assertEqual(self.apply('MINE').status_code, 200)
        self.assertEqual(self.session_code(), 'MINE')

    def test_stored_code_that_stops_being_valid_is_shown_as_a_notice_and_not_applied(self):
        coupon = make_coupon('FADE', value=20)
        self.apply('FADE')
        Coupon.objects.filter(pk=coupon.pk).update(is_active=False)
        response = self.invoice()
        self.assertEqual(response.context['final_total'], 200000)
        self.assertContains(response, 'کد تخفیف ذخیره‌شده‌ی شما اعمال نشد')
        self.assertContains(response, 'فعال نیست')

    def test_invoice_response_still_carries_the_oob_cart_rows(self):
        make_coupon('OOB', value=10)
        self.assertContains(self.apply('OOB'), 'hx-swap-oob="true"')

    # ---- ارسال رایگان ----
    def test_free_shipping_code_waives_the_courier_tariff(self):
        make_coupon('SHIP', kind='free_shipping')
        response = self.apply('SHIP', address=self.courier)
        self.assertEqual(self.session_code(), 'SHIP')
        self.assertEqual(response.context['quote'].cost, 0)
        self.assertEqual(response.context['final_total'], 200000)
        self.assertContains(response, 'ارسال رایگان')
        self.assertContains(response, '45000')                         # کرایه‌ی خط‌خورده

    def test_free_shipping_code_needs_a_courier_address(self):
        make_coupon('SHIP', kind='free_shipping')
        self.assertContains(self.apply('SHIP', address=self.address), 'فقط برای ارسال با پیک')
        self.assertIsNone(self.session_code())

    def test_free_shipping_code_when_shipping_is_already_free(self):
        self.product.free_shipping = True
        self.product.save(update_fields=['free_shipping'])
        make_coupon('SHIP', kind='free_shipping')
        self.assertContains(self.apply('SHIP', address=self.courier), 'از قبل رایگان است')

    def test_free_shipping_code_never_unblocks_an_unset_tariff(self):
        blocked = self.make_address(self.user, self.zoned_city, self.unpriced_zone, title='بدون تعرفه')
        make_coupon('SHIP', kind='free_shipping')
        response = self.apply('SHIP', address=blocked)
        self.assertFalse(response.context['quote'].available)
        self.assertIsNone(self.session_code())

    def test_free_shipping_code_follows_the_address_when_it_changes(self):
        make_coupon('SHIP', kind='free_shipping')
        self.apply('SHIP', address=self.courier)
        response = self.invoice(address=self.address)                  # کاربر آدرس پستی را انتخاب کرد
        self.assertEqual(response.context['final_total'], 200000)
        self.assertContains(response, 'فقط برای ارسال با پیک')         # اخطار، نه خطای سخت
        response = self.invoice(address=self.courier)
        self.assertEqual(response.context['quote'].cost, 0)


# ======================================================================== محدودکننده‌ی حدس
class CheckoutRateLimitTests(CouponCheckoutBase):
    def test_ten_wrong_guesses_lock_further_attempts_even_for_a_valid_code(self):
        make_coupon('REAL', value=10)
        for i in range(10):
            self.assertContains(self.apply(f'WRONG{i}'), 'کد تخفیف معتبر نیست.')
        response = self.apply('REAL')
        self.assertContains(response, 'تعداد تلاش‌های ناموفق شما از حد مجاز گذشته است')
        self.assertIsNone(self.session_code())
        reset_coupon_attempts(self.user)
        self.assertEqual(self.apply('REAL').status_code, 200)
        self.assertEqual(self.session_code(), 'REAL')

    def test_nine_wrong_guesses_do_not_lock(self):
        make_coupon('REAL', value=10)
        for i in range(9):
            self.apply(f'WRONG{i}')
        self.apply('REAL')
        self.assertEqual(self.session_code(), 'REAL')

    def test_cart_rule_failures_are_not_counted_as_guesses(self):
        make_coupon('BIG', min_cart_amount=10 ** 7)
        for _ in range(15):
            self.assertContains(self.apply('BIG'), 'حداقل مبلغ سبد')
        self.assertTrue(ratelimit.check(self.user, TEST_CLIENT_IP)[0])

    def test_expired_inactive_and_unassigned_codes_are_counted(self):
        make_coupon('E1', expired=True)
        make_coupon('E2', active=False)
        make_coupon('E3', audience='assigned')
        for code in ('E1', 'E2', 'E3', 'E1', 'E2', 'E3', 'E1', 'E2', 'E3', 'E1'):
            self.apply(code)
        self.assertFalse(ratelimit.check(self.user, TEST_CLIENT_IP)[0])

    def test_lock_is_per_user_and_per_ip(self):
        make_coupon('REAL', value=10)
        for i in range(10):
            self.apply(f'WRONG{i}')
        self.client.force_login(self.other)
        Cart.objects.create(user=self.other)
        CartItem.objects.create(cart=Cart.objects.get(user=self.other), product=self.product, quantity=1)
        other_address = self.make_address(self.other, self.post_city)
        response = self.client.post(reverse(APPLY), {'code': 'REAL', 'payment_method': 'check', 'address_id': other_address.pk})
        self.assertContains(response, 'از حد مجاز گذشته است')          # همان IP مسدود است
        response = self.client.post(reverse(APPLY), {'code': 'REAL', 'payment_method': 'check', 'address_id': other_address.pk},
                                    REMOTE_ADDR='198.51.100.77')
        self.assertContains(response, 'اعمال شد')                      # IP دیگر و کاربر دیگر آزاد است
        ratelimit.reset(None, '198.51.100.77')

    def test_forged_forwarded_header_cannot_dodge_the_ip_counter(self):
        for i in range(10):
            self.client.post(reverse(APPLY), {'code': f'W{i}', 'payment_method': 'check', 'address_id': self.address.pk},
                             HTTP_X_FORWARDED_FOR=f'203.0.113.{i}')
        self.assertFalse(ratelimit.check(None, TEST_CLIENT_IP)[0])

    def test_cap_is_configurable_in_the_policy(self):
        policy = DiscountPolicy.load()
        policy.coupon_max_invalid_attempts = 2
        policy.save()
        for i in range(2):
            self.apply(f'W{i}')
        self.assertContains(self.apply('W3'), 'از حد مجاز گذشته است')


# ======================================================================== ثبت سفارش با کد
class SubmitWithCouponTests(CouponCheckoutBase):
    def test_order_snapshots_the_coupon_and_reserves_capacity(self):
        coupon = make_coupon('SAVE20', value=20, total_limit=5)
        self.apply('SAVE20')
        response = self.place()
        self.assertEqual(response.status_code, 302)
        order = self.order()
        self.assertEqual((order.order_discount, order.order_discount_label, order.coupon_code), (40000, 'کد تخفیف SAVE20', 'SAVE20'))
        self.assertEqual(order.total_price, 160000)
        self.assertEqual(order.total_price, order.computed_total)
        self.assertEqual(order.items.get().price, 100000)              # قیمت ردیف بدون کوپن؛ کوپن سطح سفارش است
        redemption = CouponRedemption.objects.get(order_id=order.pk)
        self.assertEqual((redemption.coupon_id, redemption.user_id, redemption.status, redemption.code, redemption.discount_amount),
                         (coupon.pk, self.user.pk, 'reserved', 'SAVE20', 40000))
        self.assertEqual(redemption.expires_at - redemption.reserved_at, timedelta(minutes=30))
        self.assertIsNone(self.session_code())                          # کد مصرف شد و از نشست پاک شد
        self.assertFalse(Cart.objects.filter(user=self.user).exists())

    def test_order_without_a_code_has_no_coupon_snapshot(self):
        self.place()
        order = self.order()
        self.assertEqual((order.order_discount, order.order_discount_label, order.coupon_code, order.shipping_discount), (0, '', '', 0))
        self.assertFalse(CouponRedemption.objects.exists())

    def test_coupon_combines_with_automatic_discount_totals(self):
        second = self.product.__class__.objects.create(name='دوم', slug='coupon-second', erp_code='ERP-C-2',
                                                       category=self.product.category, price=50000, price2=45000, stock=9)
        CartItem.objects.create(cart=self.cart, product=second, quantity=2)
        make_promotion(self.product, percent=10)                        # ۲×۹۰٬۰۰۰ = ۱۸۰٬۰۰۰ + ۱۰۰٬۰۰۰ = ۲۸۰٬۰۰۰
        make_coupon('TEN', value=10)                                     # فقط روی ۱۰۰٬۰۰۰ بدون تخفیف خودکار ← ۱۰٬۰۰۰
        self.apply('TEN')
        self.place()
        order = self.order()
        self.assertEqual((order.promotion_discount, order.order_discount, order.total_price), (20000, 10000, 270000))
        self.assertEqual(order.total_price, order.computed_total)

    def test_free_shipping_code_snapshot(self):
        make_coupon('SHIP', kind='free_shipping')
        self.apply('SHIP', address=self.courier)
        self.place(address=self.courier)
        order = self.order()
        self.assertEqual((order.shipping_method, order.shipping_cost, order.shipping_discount), ('courier', 0, 45000))
        self.assertEqual((order.order_discount, order.coupon_code, order.total_price), (0, 'SHIP', 200000))
        self.assertIn('کد تخفیف SHIP', order.shipping_label)
        redemption = CouponRedemption.objects.get(order_id=order.pk)
        self.assertEqual((redemption.discount_amount, redemption.shipping_discount), (0, 45000))

    def test_courier_order_pays_the_tariff_normally_with_a_goods_coupon(self):
        make_coupon('TEN', value=10)
        self.apply('TEN', address=self.courier)
        self.place(address=self.courier)
        order = self.order()
        self.assertEqual((order.total_price, order.shipping_cost, order.shipping_discount), (180000 + 45000, 45000, 0))

    def test_holoo_invoice_is_balanced_with_the_customers_payment(self):
        make_coupon('TEN', value=10)
        self.apply('TEN', address=self.courier)
        self.place(address=self.courier)
        order = self.order()
        rows = [(item, item.product.erp_code) for item in order.items.select_related('product')]
        payload = build_invoice_payload(order, item_lines(order, rows, 'ثبت'), 'SHIP-1')
        self.assertEqual(payload_total(payload), order.total_price)
        self.assertIn('کد تخفیف SAVE'.replace('SAVE', 'TEN') + ': 20000', payload['Comment'])

    def test_order_flow_lock_and_capacity_are_taken_inside_the_transaction(self):
        make_coupon('LOCKED', value=10, total_limit=1)
        self.apply('LOCKED')
        with CaptureQueriesContext(connection) as queries:
            self.place()
        sql = [q['sql'].upper() for q in queries.captured_queries]
        self.assertTrue(any('UPDLOCK' in s and 'PROMOTIONS_COUPON' in s for s in sql), 'ردیف کوپن باید قفل شود')
        self.assertTrue(any('UPDLOCK' in s and 'CART_CART' in s for s in sql), 'ردیف سبد باید قفل شود')

    def test_per_user_limit_blocks_a_second_order_with_the_same_code(self):
        make_coupon('ONCE', value=10, per_user_limit=1)
        self.apply('ONCE')
        self.place()
        cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(cart=cart, product=self.product, quantity=1)
        response = self.apply('ONCE')
        self.assertContains(response, 'قبلاً از این کد تخفیف استفاده کرده‌اید')

    def test_total_limit_across_users(self):
        make_coupon('ONE', value=10, total_limit=1, per_user_limit=None)
        self.apply('ONE')
        self.place()
        self.client.force_login(self.other)
        cart = Cart.objects.create(user=self.other)
        CartItem.objects.create(cart=cart, product=self.product, quantity=1)
        address = self.make_address(self.other, self.post_city)
        response = self.client.post(reverse(APPLY), {'code': 'ONE', 'payment_method': 'check', 'address_id': address.pk})
        self.assertContains(response, 'ظرفیت استفاده از این کد تخفیف به پایان رسیده است')

    def test_first_order_only_code(self):
        make_coupon('FIRST', value=10, first_order_only=True)
        self.apply('FIRST')
        self.place()
        cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(cart=cart, product=self.product, quantity=1)
        self.assertContains(self.apply('FIRST'), 'فقط برای اولین خرید')

    def test_double_submit_creates_one_order_and_never_a_second_use(self):
        make_coupon('DOUBLE', value=10, per_user_limit=None, total_limit=None)
        self.apply('DOUBLE')
        data = {'address_id': self.address.pk, 'payment_method': 'check', 'expected_total': 180000}
        with mock.patch('holoo.receivers.send_order_to_holoo'):
            first = self.client.post(reverse('orders:submit_order'), data)
            second = self.client.post(reverse('orders:submit_order'), data)
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)                        # سبد دیگر نیست ← به تاریخچه هدایت می‌شود
        self.assertRedirects(second, reverse('orders:order_history'), fetch_redirect_response=False)
        self.assertEqual(Order.objects.filter(user=self.user).count(), 1)
        self.assertEqual(CouponRedemption.objects.count(), 1)

    # ---- چرخه‌ی عمر تا پرداخت و لغو ----
    def test_successful_payment_turns_the_reservation_into_a_final_use(self):
        make_coupon('PAY', value=10)
        self.apply('PAY')
        self.place()
        order = self.order()
        from payments.models import Transaction
        txn = Transaction.objects.create(user=self.user, order=order, amount=order.total_price, authority='AUTH-COUPON-1')
        with mock.patch('holoo.receivers.confirm_payment_in_holoo'):
            with self.captureOnCommitCallbacks(execute=True):
                self.client.get(f"{reverse('payments:callback')}?Authority={txn.authority}&Status=OK")
        redemption = CouponRedemption.objects.get(order_id=order.pk)
        self.assertEqual((redemption.status, redemption.over_limit), ('redeemed', False))
        self.assertIsNotNone(redemption.redeemed_at)

    def test_failed_payment_keeps_the_reservation_until_it_expires(self):
        make_coupon('FAILPAY', value=10, total_limit=1, per_user_limit=None)
        self.apply('FAILPAY')
        self.place()
        order = self.order()
        from payments.models import Transaction
        txn = Transaction.objects.create(user=self.user, order=order, amount=order.total_price, authority='AUTH-COUPON-2')
        self.client.get(f"{reverse('payments:callback')}?Authority={txn.authority}&Status=NOK")
        self.assertEqual(CouponRedemption.objects.get(order_id=order.pk).status, 'reserved')

    def test_cancelling_the_order_releases_the_code(self):
        coupon = make_coupon('CANCEL', value=10, total_limit=1, per_user_limit=None)
        self.apply('CANCEL')
        self.place()
        order = self.order()
        self.assertEqual(coupons.active_uses(coupon), 1)
        with self.captureOnCommitCallbacks(execute=True):
            order.status = 'canceled'
            order.save()
        self.assertEqual(coupons.active_uses(coupon), 0)
        self.assertEqual(CouponRedemption.objects.get(order_id=order.pk).release_reason, 'order_canceled')
        cart = Cart.objects.create(user=self.user)
        CartItem.objects.create(cart=cart, product=self.product, quantity=1)
        self.assertEqual(self.apply('CANCEL').status_code, 200)         # دوباره قابل استفاده است
        self.assertEqual(self.session_code(), 'CANCEL')

    def test_an_unpaid_reservation_frees_capacity_after_the_deadline(self):
        coupon = make_coupon('EXPIRE', value=10, total_limit=1, per_user_limit=None)
        self.apply('EXPIRE')
        self.place()
        later = timezone.now() + timedelta(minutes=31)
        self.assertEqual(coupons.active_uses(coupon, now=later), 0)
        self.assertEqual(coupons.active_uses(coupon), 1)


# ======================================================================== کنترل تغییر قیمت
class PriceDriftTests(CouponCheckoutBase):
    def submit(self, expected, **extra):
        data = {'address_id': self.address.pk, 'payment_method': 'check', **extra}
        if expected is not None:
            data['expected_total'] = expected
        with mock.patch('holoo.receivers.send_order_to_holoo'):
            with self.captureOnCommitCallbacks(execute=True):
                return self.client.post(reverse('orders:submit_order'), data)

    def assertDrift(self, response, new_total):
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, PRICE_DRIFT_MESSAGE, status_code=409)
        self.assertContains(response, 'id="price-drift-notice"', status_code=409)
        self.assertContains(response, f'id="expected-total" value="{new_total}"', status_code=409)   # مبلغ به‌روز
        self.assertEqual(Order.objects.filter(user=self.user).count(), 0)
        self.assertTrue(Cart.objects.filter(user=self.user).exists())                                  # سبد دست‌نخورده

    def test_the_exact_message_is_the_one_requested(self):
        self.assertEqual(PRICE_DRIFT_MESSAGE, 'مبالغ سفارش شما به‌دلیل تغییر وضعیت تخفیف‌ها به‌روز شد؛ '
                                              'لطفاً بررسی و تأیید نهایی نمایید.')

    def test_matching_amount_places_the_order(self):
        self.assertEqual(self.submit(200000).status_code, 302)
        self.assertEqual(Order.objects.get(user=self.user).total_price, 200000)

    def test_one_rial_more_or_less_stops_the_order(self):
        for wrong in (199999, 200001):
            with self.subTest(expected=wrong):
                self.assertDrift(self.submit(wrong), 200000)

    def test_missing_or_malformed_expected_total_stops_the_order(self):
        for wrong in (None, '', 'abc', '۲۰۰۰۰۰', '200000.0', '-200000', '200 000', '2e5', '٢٠٠٠٠٠'):
            with self.subTest(expected=wrong):
                self.assertDrift(self.submit(wrong), 200000)

    def test_the_posted_amount_never_becomes_the_order_amount(self):
        """ حتی با تلاش برای «قیمت پایین‌تر»، فقط مبلغ محاسبه‌ی سرور می‌تواند ثبت شود """
        self.assertDrift(self.submit(1), 200000)
        self.assertDrift(self.submit(10 ** 9), 200000)
        self.submit(200000, total_price=1, final_total=1, shipping_cost=1)
        self.assertEqual(Order.objects.get(user=self.user).total_price, 200000)

    def test_drift_response_keeps_the_selected_address_and_payment_method(self):
        response = self.submit(1, address_id=self.courier.pk, payment_method='cash')
        self.assertEqual(response.status_code, 409)
        html = response.content.decode()
        self.assertRegex(html, rf'value="{self.courier.pk}"[^>]*checked')
        self.assertRegex(html, r'value="cash"[^>]*checked')
        self.assertNotRegex(html, r'value="check"[^>]*checked')
        self.assertEqual(response.context['final_total'], 45000 + 180000)                           # نقدی: قیمت ۲ سطح

    def test_drift_response_renders_the_fresh_invoice_inline_without_duplicating_cart_rows(self):
        html = self.submit(1).content.decode()
        self.assertIn('صورتحساب نهایی', html)
        self.assertEqual(html.count('id="checkout-cart-items"'), 1)

    def test_resubmitting_the_refreshed_amount_succeeds(self):
        make_promotion(self.product, percent=20)
        reset_promotions_cache()
        first = self.submit(200000)                                       # کاربر فاکتور قدیمی (بدون تخفیف) را دیده بود
        self.assertDrift(first, 160000)
        self.assertEqual(self.submit(160000).status_code, 302)
        self.assertEqual(Order.objects.get(user=self.user).total_price, 160000)

    def test_promotion_expiring_between_invoice_and_confirm(self):
        promotion = make_promotion(self.product, percent=20)
        shown = int(self.invoice().context['final_total'])
        self.assertEqual(shown, 160000)
        after = promotion.ends_at + timedelta(seconds=1)
        reset_promotions_cache()
        with mock.patch('django.utils.timezone.now', return_value=after):
            response = self.submit(shown)
        self.assertDrift(response, 200000)

    def test_promotion_starting_between_invoice_and_confirm(self):
        promotion = make_promotion(self.product, percent=20, scheduled=True)
        self.assertEqual(int(self.invoice().context['final_total']), 200000)
        during = promotion.starts_at + timedelta(minutes=1)
        with mock.patch('django.utils.timezone.now', return_value=during):
            self.assertDrift(self.submit(200000), 160000)

    def test_product_price_change_between_invoice_and_confirm(self):
        shown = int(self.invoice().context['final_total'])
        self.product.price = 110000
        self.product.save(update_fields=['price'])
        self.assertDrift(self.submit(shown), 220000)

    def test_free_shipping_rule_ending_between_invoice_and_confirm(self):
        rule = make_free_shipping_rule(min_total=100_000, ends_at=timezone.now() + timedelta(hours=1))
        shown = int(self.invoice(address=self.courier).context['final_total'])
        self.assertEqual(shown, 200000)                                   # ارسال رایگان با قاعده
        with mock.patch('django.utils.timezone.now', return_value=rule.ends_at + timedelta(seconds=1)):
            self.assertDrift(self.submit(shown, address_id=self.courier.pk), 245000)

    def test_coupon_expiring_between_invoice_and_confirm_explains_why(self):
        coupon = make_coupon('SOON', value=20, ends_at=timezone.now() + timedelta(hours=1))
        self.apply('SOON')
        shown = int(self.invoice().context['final_total'])
        self.assertEqual(shown, 160000)
        with mock.patch('django.utils.timezone.now', return_value=coupon.ends_at + timedelta(seconds=1)):
            response = self.submit(shown)
        self.assertDrift(response, 200000)
        self.assertContains(response, 'کد تخفیف شما دیگر قابل اعمال نیست', status_code=409)
        self.assertContains(response, 'به پایان رسیده است', status_code=409)
        self.assertEqual(CouponRedemption.objects.count(), 0)

    def test_coupon_capacity_taken_by_someone_else_between_invoice_and_confirm(self):
        coupon = make_coupon('LAST', value=20, total_limit=1, per_user_limit=None)
        self.apply('LAST')
        shown = int(self.invoice().context['final_total'])
        stranger = CustomUser.objects.create_user(phone_number='09120000555')
        other_order = Order.objects.create(user=stranger, first_name='ب', last_name='ج', phone='09120000555', address='x', total_price=1)
        CouponRedemption.objects.create(coupon=coupon, user=stranger, order_id=other_order.pk, code='LAST',
                                        status='redeemed', discount_amount=1000)
        response = self.submit(shown)
        self.assertDrift(response, 200000)
        self.assertContains(response, 'ظرفیت استفاده از این کد تخفیف به پایان رسیده است', status_code=409)

    def test_no_side_effects_on_drift(self):
        coupon = make_coupon('CLEAN', value=20, total_limit=3)
        self.apply('CLEAN')
        self.submit(1)
        self.assertEqual((CouponRedemption.objects.count(), coupons.active_uses(coupon)), (0, 0))
        self.assertEqual(self.session_code(), 'CLEAN')                    # کد در نشست می‌ماند تا کاربر دوباره تأیید کند

    def test_forged_client_time_headers_cannot_change_the_result(self):
        promotion = make_promotion(self.product, percent=20)
        reset_promotions_cache()
        with mock.patch('django.utils.timezone.now', return_value=promotion.ends_at + timedelta(days=1)):
            response = self.submit(160000, HTTP_DATE='Sat, 01 Jan 2000 00:00:00 GMT', HTTP_X_CLIENT_TIME='2000-01-01',
                                   client_time='2000-01-01T00:00:00', now='2000-01-01')
        self.assertDrift(response, 200000)

    # ---- اتمیک بودن ----
    def test_failure_while_reserving_the_code_rolls_back_the_whole_order(self):
        make_coupon('ATOMIC', value=10)
        self.apply('ATOMIC')
        with mock.patch('promotions.coupons.reserve', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                self.submit(180000)
        self.assertEqual(Order.objects.filter(user=self.user).count(), 0)
        self.assertEqual(CouponRedemption.objects.count(), 0)
        self.assertTrue(Cart.objects.filter(user=self.user).exists())
        self.assertEqual(self.session_code(), 'ATOMIC')

    def test_failure_while_creating_items_rolls_back_the_order_and_the_reservation(self):
        make_coupon('ATOMIC2', value=10)
        self.apply('ATOMIC2')
        with mock.patch('orders.views.OrderItem.objects.bulk_create', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                self.submit(180000)
        self.assertEqual(Order.objects.filter(user=self.user).count(), 0)
        self.assertEqual(CouponRedemption.objects.count(), 0)
        self.assertTrue(Cart.objects.filter(user=self.user).exists())

    def test_blocked_address_is_refused_before_the_amount_check(self):
        blocked = self.make_address(self.user, self.zoned_city, self.unpriced_zone, title='بدون تعرفه')
        response = self.submit(200000, address_id=blocked.pk)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'تعرفه ارسال به این ناحیه هنوز تعیین نشده است')
        self.assertEqual(Order.objects.filter(user=self.user).count(), 0)


# ======================================================================== قاعده‌ی ارسال رایگان در تسویه
class FreeShippingRuleCheckoutTests(CouponCheckoutBase):
    def test_courier_is_free_at_the_threshold_and_the_label_says_why(self):
        make_free_shipping_rule(min_total=200_000)
        response = self.invoice(address=self.courier)
        self.assertEqual((response.context['quote'].cost, response.context['final_total']), (0, 200000))
        self.assertContains(response, 'ارسال رایگان (خرید بالای 200000 تومان)')
        self.assertContains(response, 'رایگان')

    def test_below_the_threshold_pays_the_tariff(self):
        make_free_shipping_rule(min_total=200_001)
        self.assertEqual(self.invoice(address=self.courier).context['final_total'], 245000)

    def test_threshold_is_measured_after_the_goods_coupon(self):
        make_free_shipping_rule(min_total=190_000)
        make_coupon('TEN', value=10)                                       # ۲۰۰٬۰۰۰ ← ۱۸۰٬۰۰۰ < ۱۹۰٬۰۰۰
        self.assertEqual(self.invoice(address=self.courier).context['quote'].cost, 0)
        self.apply('TEN', address=self.courier)
        response = self.invoice(address=self.courier)
        self.assertEqual((response.context['quote'].cost, response.context['final_total']), (45000, 180000 + 45000))

    def test_threshold_is_measured_after_automatic_discounts(self):
        make_free_shipping_rule(min_total=200_000)
        make_promotion(self.product, percent=10)                            # ۱۸۰٬۰۰۰
        self.assertEqual(self.invoice(address=self.courier).context['quote'].cost, 45000)

    def test_order_snapshot_of_a_rule_free_courier_shipment(self):
        make_free_shipping_rule(min_total=100_000)
        self.place(address=self.courier)
        order = self.order()
        self.assertEqual((order.shipping_method, order.shipping_cost, order.shipping_discount), ('courier', 0, 45000))
        self.assertIn('ارسال رایگان (خرید بالای 100000 تومان)', order.shipping_label)
        self.assertEqual(order.total_price, 200000)
        self.assertEqual(order.total_price, order.computed_total)

    def test_postage_cover_rule_shows_a_free_post_label_in_checkout(self):
        make_free_shipping_rule(min_total=100_000, postage_mode='cover')
        response = self.invoice(address=self.address)
        self.assertContains(response, 'ارسال رایگان با پست (خرید بالای 100000 تومان)')
        self.place(address=self.address)
        order = self.order()
        self.assertEqual((order.shipping_method, order.shipping_cost), ('post', 0))
        self.assertIn('ارسال رایگان با پست', order.shipping_label)

    def test_unset_tariff_blocks_in_checkout_even_with_a_matching_rule(self):
        make_free_shipping_rule()
        blocked = self.make_address(self.user, self.zoned_city, self.unpriced_zone, title='بدون تعرفه')
        response = self.invoice(address=blocked)
        self.assertFalse(response.context['quote'].available)
        self.assertContains(response, 'امکان ثبت سفارش با این آدرس وجود ندارد')

    def test_address_cards_reflect_the_rule(self):
        make_free_shipping_rule(min_total=100_000)
        response = self.client.get(reverse('orders:checkout'))
        labels = {option['address'].pk: option['quote'].label for option in response.context['address_options']}
        self.assertEqual(labels[self.courier.pk], 'ارسال رایگان (خرید بالای 100000 تومان)')


# ======================================================================== سیاست سراسری ارسال رایگان (ادمین)
class FreeShippingPolicySwitchTests(CouponCheckoutBase):
    """ کلید سراسری، مبنای سنجش حداقل مبلغ (پس از کد تخفیفِ کالا یا نه) و تغییر حداقل مبلغ توسط ادمین """

    def policy(self, **fields):
        policy = DiscountPolicy.load()
        for name, value in fields.items():
            setattr(policy, name, value)
        policy.save()

    def courier_total(self):
        return int(self.invoice(address=self.courier).context['final_total'])

    def test_global_switch_off_disables_every_rule_and_on_restores_it_immediately(self):
        make_free_shipping_rule(min_total=100_000)
        self.assertEqual(self.courier_total(), 200000)                        # رایگان
        self.policy(free_shipping_rules_enabled=False)
        response = self.invoice(address=self.courier)
        self.assertEqual((response.context['quote'].cost, response.context['final_total']), (45000, 245000))
        self.assertEqual(response.context['quote'].label, 'ارسال با پیک')
        self.policy(free_shipping_rules_enabled=True)
        self.assertEqual(self.courier_total(), 200000)

    def test_switch_off_also_hides_the_rule_on_the_address_cards_and_for_postage(self):
        make_free_shipping_rule(min_total=100_000, postage_mode='cover')
        self.policy(free_shipping_rules_enabled=False)
        page = self.client.get(reverse('orders:checkout'))
        labels = {o['address'].pk: o['quote'].label for o in page.context['address_options']}
        self.assertEqual(labels[self.courier.pk], 'ارسال با پیک')
        self.assertNotIn('ارسال رایگان', labels[self.address.pk])
        self.assertEqual(self.invoice(address=self.address).context['quote'].label, 'پس‌کرایه (پرداخت هزینه درب منزل)')

    def test_switch_does_not_touch_the_cart_flag_or_the_free_shipping_coupon(self):
        self.policy(free_shipping_rules_enabled=False)
        make_coupon('SHIP', kind='free_shipping')
        self.apply('SHIP', address=self.courier)
        self.assertEqual(self.invoice(address=self.courier).context['quote'].cost, 0)              # کوپن ارسال رایگان
        self.remove(address=self.courier)
        self.product.free_shipping = True
        self.product.save(update_fields=['free_shipping'])
        self.assertEqual(self.invoice(address=self.courier).context['quote'].free_source, 'cart')  # پرچم کالا

    def test_threshold_measured_after_the_goods_coupon_by_default(self):
        make_free_shipping_rule(min_total=190_000)
        make_coupon('TEN', value=10)                                            # ۲۰۰٬۰۰۰ ← ۱۸۰٬۰۰۰
        self.apply('TEN', address=self.courier)
        self.assertEqual(self.courier_total(), 180000 + 45000)                 # زیر ۱۹۰٬۰۰۰ ← پیک پولی

    def test_threshold_can_ignore_the_goods_coupon(self):
        make_free_shipping_rule(min_total=190_000)
        make_coupon('TEN', value=10)
        self.policy(free_shipping_threshold_after_coupon=False)
        self.apply('TEN', address=self.courier)
        response = self.invoice(address=self.courier)
        self.assertEqual((response.context['quote'].cost, response.context['final_total']), (0, 180000))   # مبنا: ۲۰۰٬۰۰۰ پیش از کد
        self.assertEqual(response.context['quote'].free_source, 'rule')

    def test_threshold_policy_still_measures_after_automatic_discounts(self):
        make_free_shipping_rule(min_total=190_000)
        make_promotion(self.product, percent=10)                                 # ۱۸۰٬۰۰۰
        self.policy(free_shipping_threshold_after_coupon=False)
        self.assertEqual(self.courier_total(), 180000 + 45000)

    def test_editing_the_minimum_amount_takes_effect_immediately(self):
        rule = make_free_shipping_rule(min_total=300_000)
        self.assertEqual(self.courier_total(), 245000)
        rule.min_cart_total = 200_000
        rule.save()
        self.assertEqual(self.courier_total(), 200000)
        rule.is_active = False
        rule.save()
        self.assertEqual(self.courier_total(), 245000)

    def test_toggling_the_switch_between_invoice_and_confirm_is_caught_as_price_drift(self):
        make_free_shipping_rule(min_total=100_000)
        shown = self.courier_total()
        self.assertEqual(shown, 200000)
        self.policy(free_shipping_rules_enabled=False)
        data = {'address_id': self.courier.pk, 'payment_method': 'check', 'expected_total': shown}
        with mock.patch('holoo.receivers.send_order_to_holoo'):
            response = self.client.post(reverse('orders:submit_order'), data)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.context['final_total'], 245000)
        self.assertEqual(Order.objects.filter(user=self.user).count(), 0)

    def test_policy_defaults_are_safe(self):
        policy = DiscountPolicy.load()
        self.assertTrue(policy.free_shipping_rules_enabled)
        self.assertTrue(policy.free_shipping_threshold_after_coupon)

    def test_config_helper_reads_the_cached_policy_without_queries(self):
        from promotions import free_shipping
        free_shipping.get_config()
        with self.assertNumQueries(0):
            self.assertEqual(free_shipping.get_config(), (True, True))
        self.policy(free_shipping_rules_enabled=False, free_shipping_threshold_after_coupon=False)
        self.assertEqual(free_shipping.get_config(), (False, False))
        self.assertEqual(free_shipping.enabled_rules(), ())
