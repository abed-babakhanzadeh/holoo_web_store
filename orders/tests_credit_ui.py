"""
فاز G2: تجربه‌ی کاربری سقف اعتبار چکی — رادیوی چکیِ غیرفعال با توضیح مانده/مبلغ/مازاد در تسویه‌حساب، سوئیچ خودکار به نقدی، هم‌گامی زنده
با فاکتور (HTMX/OOB: آدرس، کد تخفیف، تعداد)، رندر دوباره‌ی ۴۰۹ هماهنگ، و کارت سقف/مصرف/مانده در پنل «درخواست خرید چکی».
"""
import re
from unittest import mock

from django.test import Client
from django.urls import reverse

from accounts.models import CustomUser
from accounts.testing import make_approved_user
from accounts.tests_cheque_credit import valid_national_code
from cart.models import Cart, CartItem
from orders import credit, payment_options
from orders.models import Order
from orders.tests import CheckoutTestBase
from orders.tests_credit_limit import make_order, set_limit
from promotions.testing import make_coupon

RADIO = re.compile(r'<input type="radio" name="payment_method"[^>]*>')


def radios(html):
    """ {value: {'checked': bool, 'disabled': bool}} برای رادیوهای روش پرداخت """
    found = {}
    for tag in RADIO.findall(html):
        value = re.search(r'value="([^"]+)"', tag).group(1)
        found[value] = {'checked': ' checked' in tag, 'disabled': ' disabled' in tag}
    return found


def status_tag(html):
    match = re.search(r'<div id="credit-status"[^>]*>', html)
    return match.group(0) if match else None


class UiBase(CheckoutTestBase):
    TOTAL = 200000                 # ۲ × ۱۰۰٬۰۰۰ (قیمت چکی)، پست ← کرایه ۰
    CASH = 180000                  # ۲ × ۹۰٬۰۰۰

    def checkout(self, **params):
        return self.client.get(reverse('orders:checkout'), params)

    def invoice(self, **params):
        return self.client.get(reverse('orders:update_invoice'), params)

    def blocked(self, response):
        tag = status_tag(response.content.decode())
        return None if tag is None else 'data-blocked="1"' in tag


class CheckoutRenderTests(UiBase):
    def test_a_user_without_a_limit_sees_no_credit_box_and_a_normal_cheque_radio(self):
        html = self.checkout().content.decode()
        self.assertIsNone(status_tag(html))
        self.assertEqual(radios(html), {'check': {'checked': True, 'disabled': False}, 'cash': {'checked': False, 'disabled': False}})

    def test_an_unlimited_user_costs_no_extra_pricing(self):
        with mock.patch('orders.views.compute_checkout') as extra:
            self.checkout()
        extra.assert_not_called()

    def test_within_the_limit_the_radio_stays_enabled_and_the_box_is_green(self):
        set_limit(self.user, 1_000_000)
        html = self.checkout().content.decode()
        self.assertEqual(radios(html)['check'], {'checked': True, 'disabled': False})
        self.assertIs(self.blocked(self.checkout()), False)
        self.assertIn('1,000,000', html)
        self.assertIn('در اعتبار شما می‌گنجد', html)
        self.assertIn('data-testid="credit-status"', html)

    def test_over_the_limit_the_cheque_radio_is_disabled_and_cash_is_selected(self):
        set_limit(self.user, 150_000)
        make_order(self.user, 100_000, status='shipped')                         # مصرف‌شده
        html = self.checkout().content.decode()
        found = radios(html)
        self.assertEqual(found['check'], {'checked': False, 'disabled': True})
        self.assertEqual(found['cash'], {'checked': True, 'disabled': False})   # سوئیچ خودکار (سمت سرور)
        self.assertTrue(self.blocked(self.checkout()))
        for text in ('150,000', '100,000', '50,000', '200,000', 'مازاد: <b>150,000', 'روش نقدی را انتخاب کنید'):
            self.assertIn(text, html)

    def test_the_numbers_are_limit_used_remaining_amount_and_excess(self):
        set_limit(self.user, 250_000)
        make_order(self.user, 100_000)
        html = self.checkout().content.decode()
        box = html[html.index('id="credit-status"'):]
        box = box[:box.index('</div>\n</div>') if '</div>\n</div>' in box else 1200]
        for text in ('سقف اعتبار چکی: <b>250,000</b>', 'مصرف‌شده: <b>100,000</b>', 'مانده: <b>150,000</b>',
                     'مبلغ کل این سفارش با روش چکی: <b>200,000</b>', 'مازاد: <b>50,000</b>'):
            self.assertIn(text, box)

    def test_an_exactly_fitting_order_is_not_blocked(self):
        set_limit(self.user, self.TOTAL)
        self.assertIs(self.blocked(self.checkout()), False)
        set_limit(self.user, self.TOTAL - 1)
        self.assertIs(self.blocked(self.checkout()), True)

    def test_a_frozen_limit_disables_the_cheque_radio_with_a_clear_message(self):
        set_limit(self.user, 0)
        html = self.checkout().content.decode()
        self.assertTrue(radios(html)['check']['disabled'])
        self.assertTrue(radios(html)['cash']['checked'])
        self.assertIn('فریز', html)
        self.assertNotIn('مازاد', html[html.index('id="credit-status"'):html.index('id="credit-status"') + 600])

    def test_a_cancelled_order_gives_the_credit_back_on_the_page(self):
        set_limit(self.user, 250_000)
        order = make_order(self.user, 100_000)
        self.assertTrue(self.blocked(self.checkout()))
        order.status = 'canceled'
        order.save()
        self.assertIs(self.blocked(self.checkout()), False)

    def test_exactly_one_credit_box_is_in_the_full_page(self):
        set_limit(self.user, 1)
        self.assertEqual(self.checkout().content.decode().count('<div id="credit-status"'), 1)

    def test_the_page_script_syncs_the_radio_with_every_oob_update(self):
        html = self.checkout().content.decode()
        for needle in ('syncCreditGate', 'htmx:oobAfterSwap', 'data-blocked', "data-cheque"):
            self.assertIn(needle, html)

    def test_a_cash_customer_with_the_permission_is_judged_on_the_cheque_price(self):
        CustomUser.objects.filter(pk=self.user.pk).update(price_level=2, can_purchase_with_check=True, cheque_credit_limit=190_000)
        html = self.checkout().content.decode()
        self.assertEqual(radios(html)['check'], {'checked': False, 'disabled': True})    # ۲۰۰٬۰۰۰ > ۱۹۰٬۰۰۰ حتی اگر نقدی ۱۸۰٬۰۰۰ جا شود
        self.assertTrue(radios(html)['cash']['checked'])
        self.assertIn('200,000', html)


class LiveInvoiceTests(UiBase):
    def test_the_invoice_response_carries_an_oob_credit_status(self):
        set_limit(self.user, 150_000)
        response = self.invoice(payment_method='cash', address_id=self.address.pk)
        tag = status_tag(response.content.decode())
        self.assertIn('hx-swap-oob="true"', tag)
        self.assertIn('data-blocked="1"', tag)
        self.assertContains(response, 'id="expected-total" value="180000"')          # فاکتور برای روش انتخاب‌شده (نقدی)

    def test_no_oob_box_for_unlimited_users(self):
        self.assertNotContains(self.invoice(payment_method='check', address_id=self.address.pk), 'credit-status')

    def test_the_cheque_amount_is_used_whatever_method_is_selected(self):
        set_limit(self.user, 190_000)
        for method in ('cash', 'check'):
            with self.subTest(method=method):
                self.assertIs(self.blocked(self.invoice(payment_method=method, address_id=self.address.pk)), True)

    def test_changing_the_address_re_evaluates_the_gate(self):
        set_limit(self.user, self.TOTAL)
        courier = self.make_address(self.user, self.zoned_city, zone=self.zone, title='پیک')
        self.assertIs(self.blocked(self.invoice(payment_method='check', address_id=self.address.pk)), False)
        response = self.invoice(payment_method='check', address_id=courier.pk)           # کرایه‌ی پیک ۴۵٬۰۰۰ مبلغ را بالا می‌برد
        self.assertIs(self.blocked(response), True)
        self.assertContains(response, 'مازاد: <b>45,000</b>')

    def test_changing_the_cart_re_evaluates_the_gate(self):
        set_limit(self.user, 150_000)
        self.assertIs(self.blocked(self.invoice(payment_method='check', address_id=self.address.pk)), True)
        CartItem.objects.filter(cart__user=self.user).update(quantity=1)
        self.assertIs(self.blocked(self.invoice(payment_method='check', address_id=self.address.pk)), False)

    def test_applying_a_coupon_re_evaluates_the_gate(self):
        make_coupon('SAVE10', value=10, total_limit=None, per_user_limit=None)
        set_limit(self.user, 190_000)
        self.assertIs(self.blocked(self.invoice(payment_method='check', address_id=self.address.pk)), True)
        response = self.client.post(reverse('orders:apply_coupon'),
                                    {'code': 'SAVE10', 'payment_method': 'check', 'address_id': self.address.pk})
        self.assertIs(self.blocked(response), False)                                    # ۲۰۰٬۰۰۰ − ۱۰٪ = ۱۸۰٬۰۰۰ ≤ ۱۹۰٬۰۰۰
        response = self.client.post(reverse('orders:remove_coupon'), {'payment_method': 'check', 'address_id': self.address.pk})
        self.assertIs(self.blocked(response), True)

    def test_a_used_up_credit_blocks_even_a_cheap_cart(self):
        set_limit(self.user, 100_000)
        make_order(self.user, 100_000)
        CartItem.objects.filter(cart__user=self.user).update(quantity=1)
        self.assertIs(self.blocked(self.invoice(payment_method='check', address_id=self.address.pk)), True)


class ConflictRenderTests(UiBase):
    def place(self, **extra):
        data = {'address_id': self.address.pk, 'payment_method': 'check'}
        data.update(extra)
        with mock.patch('holoo.receivers.send_order_to_holoo'):
            return self.post_order(data)

    def test_the_409_page_shows_the_cheque_closed_cash_selected_and_a_matching_invoice(self):
        set_limit(self.user, 150_000)
        response = self.place()
        self.assertEqual(response.status_code, 409)
        html = response.content.decode()
        found = radios(html)
        self.assertEqual(found['check'], {'checked': False, 'disabled': True})
        self.assertTrue(found['cash']['checked'])
        self.assertIn('id="expected-total" value="180000"', html)                  # فاکتور برای نقدی (نه چکی)
        self.assertIn('مازاد', html)
        self.assertEqual(html.count('<div id="credit-status"'), 1)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(Cart.objects.filter(user=self.user).count(), 1)

    def test_the_price_drift_page_is_consistent_when_the_cheque_is_blocked(self):
        set_limit(self.user, 150_000)
        response = self.place(expected_total=12345)
        self.assertEqual(response.status_code, 409)
        html = response.content.decode()
        self.assertIn('id="expected-total" value="180000"', html)
        self.assertTrue(radios(html)['cash']['checked'])
        self.assertEqual(Order.objects.count(), 0)

    def test_an_unblocked_conflict_page_is_unchanged(self):
        set_limit(self.user, 1_000_000)
        response = self.place(expected_total=12345)                                   # نوسان قیمت، بدون مسدودی اعتبار
        self.assertEqual(response.status_code, 409)
        html = response.content.decode()
        self.assertTrue(radios(html)['check']['checked'])
        self.assertIn('id="expected-total" value="200000"', html)

    def test_after_switching_to_cash_the_order_goes_through(self):
        set_limit(self.user, 150_000)
        self.assertEqual(self.place().status_code, 409)
        response = self.place(payment_method='cash')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Order.objects.get().total_price, self.CASH)


class HelperTests(UiBase):
    def test_option_helpers(self):
        self.assertEqual(payment_options.cheque_order_option(self.user).key, 'check')
        self.assertEqual(payment_options.online_fallback_option(self.user).key, 'cash')
        cash_customer = make_approved_user('09120000701', price_level=2)
        self.assertIsNone(payment_options.cheque_order_option(cash_customer))
        self.assertEqual(payment_options.online_fallback_option(cash_customer).key, 'cash')

    def test_credit_check_is_none_without_a_limit_and_computes_blocked_and_excess(self):
        self.assertIsNone(credit.credit_check(self.user, 10 ** 9))
        set_limit(self.user, 1000)
        check = credit.credit_check(CustomUser.objects.get(pk=self.user.pk), 1500)
        self.assertEqual((check.blocked, check.excess), (True, 500))
        self.assertFalse(credit.credit_check(CustomUser.objects.get(pk=self.user.pk), 1000).blocked)


class PanelCardTests(CheckoutTestBase):
    def setUp(self):
        super().setUp()
        CustomUser.objects.filter(pk=self.user.pk).update(price_level=2, can_purchase_with_check=True,
                                                          national_code=valid_national_code())
        self.url = reverse('accounts:cheque_credit')

    def page(self):
        return self.client.get(self.url)

    def test_no_card_without_a_limit(self):
        self.assertNotContains(self.page(), 'data-testid="credit-card"')

    def test_the_card_shows_limit_used_and_remaining_with_a_progress_bar(self):
        set_limit(self.user, 1_000_000)
        make_order(self.user, 250_000)
        page = self.page()
        self.assertContains(page, 'data-testid="credit-card"')
        self.assertContains(page, 'data-testid="credit-limit">1,000,000 تومان')
        self.assertContains(page, 'data-testid="credit-used">250,000 تومان')
        self.assertContains(page, 'data-testid="credit-remaining">750,000 تومان')
        self.assertContains(page, 'width: 25%')
        self.assertContains(page, 'aria-valuenow="25"')

    def test_a_full_credit_turns_the_card_red(self):
        set_limit(self.user, 100_000)
        make_order(self.user, 100_000)
        page = self.page()
        self.assertContains(page, 'width: 100%')
        self.assertContains(page, 'text-red-600')
        self.assertContains(page, 'data-testid="credit-remaining">0 تومان')

    def test_an_overdrawn_credit_is_capped_at_100_percent(self):
        set_limit(self.user, 100_000)
        make_order(self.user, 300_000)                                                  # سقف بعد از ثبت سفارش‌ها پایین آمده
        page = self.page()
        self.assertContains(page, 'width: 100%')
        self.assertContains(page, 'data-testid="credit-remaining">0 تومان')

    def test_a_frozen_limit_shows_the_freeze_notice(self):
        set_limit(self.user, 0)
        make_order(self.user, 40_000)
        page = self.page()
        self.assertContains(page, 'فریز شده است')
        self.assertContains(page, '40,000')
        self.assertNotContains(page, 'role="progressbar"')

    def test_cancelling_an_order_updates_the_card(self):
        set_limit(self.user, 500_000)
        order = make_order(self.user, 200_000)
        self.assertContains(self.page(), 'data-testid="credit-used">200,000 تومان')
        order.status = 'canceled'
        order.save()
        self.assertContains(self.page(), 'data-testid="credit-used">0 تومان')

    def test_other_users_orders_are_not_shown(self):
        set_limit(self.user, 500_000)
        make_order(self.other, 400_000)
        self.assertContains(self.page(), 'data-testid="credit-used">0 تومان')
