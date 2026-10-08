"""
فاز A چکی: تفکیک «ستون قیمت» (Order.payment_method) از «روش تسویه» (Order.settlement) و گزینه‌های پرداخت هر نوع مشتری.

ماتریس: مشتری چکی (سطح ۱) = چکی+نقدی با چکی پیش‌فرض؛ مشتری نقدی (سطح ۲) = فقط نقدی + «درخواست چکی» (مگر مجوز فردی داشته باشد)؛
مشتری ویژه (سطح ۳+) = پیش‌فرض آنلاین با قیمت ویژه + چکی طبق SiteSettings.vip_cheque_policy، با اولویتِ مجوز فردی
(قیمت ویژه‌ی خودش). سرور هر مقدار غیرمجاز را رد می‌کند؛ «درخواست چکی» سفارش نمی‌سازد.
"""
import importlib
from unittest import mock

from django.apps import apps as django_apps
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from accounts.testing import make_approved_user
from cart.models import CartItem
from orders import payment_options as po
from orders.models import Order
from orders.stock_hooks import hold_expiry
from orders.tests import CheckoutTestBase
from payments.models import Transaction
from products.models import Product, SiteSettings
from products.pricing import (
    CASH, CHECK, VIP, VIP_CHEQUE_DISABLED, VIP_CHEQUE_REQUEST, VIP_CHEQUE_STANDARD_PRICE, VIP_CHEQUE_VIP_PRICE, final_price,
    resolve_payment_method, vip_cheque_policy,
)
from promotions.models import DiscountPolicy
from promotions.testing import make_promotion


def set_policy(value):
    settings_obj = SiteSettings.load()
    settings_obj.vip_cheque_policy = value
    settings_obj.save()                                   # سیگنال ذخیره کش را باطل می‌کند
    cache.delete(SiteSettings.CACHE_KEY)


class MatrixBase(TestCase):
    def setUp(self):
        self.addCleanup(cache.delete, SiteSettings.CACHE_KEY)
        set_policy(VIP_CHEQUE_DISABLED)
        self._n = 0

    def user(self, level, flag=False):
        self._n += 1
        user = make_approved_user(f'0912111{level:02d}{self._n:02d}', price_level=level)
        if flag:
            user.can_purchase_with_check = True
            user.save(update_fields=['can_purchase_with_check'])
        return user

    def keys(self, user):
        return [o.key for o in po.available_options(user)]


class OptionMatrixTests(MatrixBase):
    def test_the_cheque_customer_sees_cheque_first_then_cash(self):
        user = self.user(1)
        self.assertEqual(self.keys(user), ['check', 'cash'])
        default = po.default_option(user)
        self.assertEqual((default.key, default.price_basis, default.settlement), ('check', CHECK, Order.SETTLEMENT_CHEQUE))
        cash = po.resolve_option(user, 'cash').option
        self.assertEqual((cash.price_basis, cash.settlement), (CASH, Order.SETTLEMENT_ONLINE))
        self.assertIsNone(po.request_option(user))

    def test_the_cash_customer_sees_only_cash_plus_a_request_option(self):
        user = self.user(2)
        self.assertEqual(self.keys(user), ['cash', 'request_check'])
        self.assertEqual([o.key for o in po.order_options(user)], ['cash'])
        self.assertEqual(po.default_option(user).key, 'cash')
        self.assertEqual(po.request_option(user).kind, po.KIND_REQUEST)

    def test_the_individual_permission_opens_cheque_for_the_cash_customer_at_the_cheque_price(self):
        user = self.user(2, flag=True)
        self.assertEqual(self.keys(user), ['cash', 'check'])
        option = po.resolve_option(user, 'check').option
        self.assertEqual((option.price_basis, option.settlement), (CHECK, Order.SETTLEMENT_CHEQUE))
        self.assertEqual(po.default_option(user).key, 'cash')                     # پیش‌فرض همچنان نقدی

    def test_the_vip_customer_defaults_to_online_with_every_policy(self):
        for policy in (VIP_CHEQUE_DISABLED, VIP_CHEQUE_VIP_PRICE, VIP_CHEQUE_STANDARD_PRICE, VIP_CHEQUE_REQUEST):
            with self.subTest(policy=policy):
                set_policy(policy)
                for flag in (False, True):
                    option = po.default_option(self.user(3, flag=flag))
                    self.assertEqual((option.key, option.price_basis, option.settlement), ('vip', VIP, Order.SETTLEMENT_ONLINE))

    def test_vip_policy_disabled_gives_online_only(self):
        user = self.user(3)
        self.assertEqual(self.keys(user), ['vip'])
        self.assertIsNone(po.request_option(user))

    def test_vip_policy_direct_vip_price(self):
        set_policy(VIP_CHEQUE_VIP_PRICE)
        user = self.user(4)
        self.assertEqual(self.keys(user), ['vip', 'vip_check'])
        option = po.resolve_option(user, 'vip_check').option
        self.assertEqual((option.price_basis, option.settlement), (VIP, Order.SETTLEMENT_CHEQUE))

    def test_vip_policy_direct_standard_price_drops_to_the_cheque_price(self):
        set_policy(VIP_CHEQUE_STANDARD_PRICE)
        user = self.user(3)
        self.assertEqual(self.keys(user), ['vip', 'check'])
        option = po.resolve_option(user, 'check').option
        self.assertEqual((option.price_basis, option.settlement), (CHECK, Order.SETTLEMENT_CHEQUE))
        self.assertIn('قیمت مصوب چکی', option.label)

    def test_vip_policy_request_check(self):
        set_policy(VIP_CHEQUE_REQUEST)
        user = self.user(3)
        self.assertEqual(self.keys(user), ['vip', 'request_check'])
        self.assertEqual([o.key for o in po.order_options(user)], ['vip'])

    def test_the_individual_permission_beats_every_policy_and_keeps_the_vip_price(self):
        for policy in (VIP_CHEQUE_DISABLED, VIP_CHEQUE_VIP_PRICE, VIP_CHEQUE_STANDARD_PRICE, VIP_CHEQUE_REQUEST):
            with self.subTest(policy=policy):
                set_policy(policy)
                user = self.user(5, flag=True)
                self.assertEqual(self.keys(user), ['vip', 'vip_check'])            # نه check (قیمت مصوب) و نه درخواست چکی
                option = po.resolve_option(user, 'vip_check').option
                self.assertEqual((option.price_basis, option.settlement), (VIP, Order.SETTLEMENT_CHEQUE))

    def test_the_permission_changes_nothing_for_the_cheque_customer(self):
        self.assertEqual(self.keys(self.user(1, flag=True)), ['check', 'cash'])


class ResolveOptionTests(MatrixBase):
    def test_statuses(self):
        cash_customer = self.user(2)
        self.assertEqual(po.resolve_option(cash_customer, 'cash').status, po.STATUS_OK)
        self.assertEqual(po.resolve_option(cash_customer, 'request_check').status, po.STATUS_REQUEST)
        for denied in ('check', 'vip', 'vip_check'):
            with self.subTest(denied=denied):
                resolution = po.resolve_option(cash_customer, denied)
                self.assertEqual((resolution.status, resolution.option.key), (po.STATUS_DENIED, 'cash'))
        for empty in ('', None, 'FREE', 'cheque'):
            self.assertEqual(po.resolve_option(cash_customer, empty).status, po.STATUS_DEFAULT)

    def test_a_vip_without_permission_is_denied_the_cheque_options(self):
        vip = self.user(3)
        for key in ('check', 'vip_check', 'cash'):                        # برای ویژه فقط «vip» (آنلاین) وجود دارد
            with self.subTest(key=key):
                resolution = po.resolve_option(vip, key)
                self.assertEqual((resolution.status, resolution.option.key), (po.STATUS_DENIED, 'vip'))

    def test_the_cheque_customer_is_not_denied_anything_it_could_see(self):
        user = self.user(1)
        for key in ('check', 'cash'):
            self.assertEqual(po.resolve_option(user, key).status, po.STATUS_OK)
        self.assertEqual(po.resolve_option(user, 'vip').status, po.STATUS_DENIED)


class PriceColumnTests(MatrixBase):
    """ resolve_payment_method (لایه‌ی قیمت): مشتری نقدی بدون مجوز هرگز قیمت چکی نمی‌گیرد """

    def test_cash_customer_cannot_price_as_cheque_without_permission(self):
        self.assertEqual(resolve_payment_method(self.user(2), CHECK), CASH)
        self.assertEqual(resolve_payment_method(self.user(2, flag=True), CHECK), CHECK)

    def test_vip_stays_locked_unless_policy_or_permission_allows_the_standard_price(self):
        self.assertEqual(resolve_payment_method(self.user(3), CHECK), VIP)
        set_policy(VIP_CHEQUE_STANDARD_PRICE)
        self.assertEqual(resolve_payment_method(self.user(3), CHECK), CHECK)
        self.assertEqual(resolve_payment_method(self.user(3, flag=True), CHECK), VIP)      # مجوز فردی: قیمت ویژه حفظ می‌شود

    def test_the_cheque_customer_keeps_both_columns_and_cannot_pick_the_vip_column(self):
        user = self.user(1)
        self.assertEqual((resolve_payment_method(user, CHECK), resolve_payment_method(user, CASH)), (CHECK, CASH))
        self.assertEqual(resolve_payment_method(user, VIP), CHECK)

    def test_an_unknown_policy_value_is_treated_as_disabled(self):
        settings_obj = SiteSettings.load()
        SiteSettings.objects.filter(pk=settings_obj.pk).update(vip_cheque_policy='something-else')
        cache.delete(SiteSettings.CACHE_KEY)
        self.assertEqual(vip_cheque_policy(), VIP_CHEQUE_DISABLED)


class CheckoutPageTests(CheckoutTestBase):
    def as_level(self, level, flag=False):
        self.user.price_level = level
        self.user.can_purchase_with_check = flag
        self.user.save(update_fields=['price_level', 'can_purchase_with_check'])

    def radios(self, response):
        import re
        html = response.content.decode()
        return re.findall(r'type="radio" name="payment_method" value="(\w+)"', html), html      # به ترتیب نمایش، فقط رادیوها

    def test_the_cheque_customer_sees_both_radios_and_no_request_link(self):
        radios, html = self.radios(self.client.get(reverse('orders:checkout')))
        self.assertEqual(radios, ['check', 'cash'])
        self.assertNotIn('request-check-link', html)

    def test_the_cash_customer_sees_cash_and_a_link_instead_of_a_cheque_radio(self):
        self.as_level(2)
        radios, html = self.radios(self.client.get(reverse('orders:checkout')))
        self.assertEqual(radios, ['cash'])
        self.assertIn('request-check-link', html)
        self.assertIn(reverse('accounts:soon_check_request'), html)

    def test_the_permitted_cash_customer_gets_the_cheque_radio_and_no_request_link(self):
        self.as_level(2, flag=True)
        radios, html = self.radios(self.client.get(reverse('orders:checkout')))
        self.assertEqual(radios, ['cash', 'check'])
        self.assertNotIn('request-check-link', html)

    def test_a_plain_vip_gets_a_hidden_field_not_a_box(self):
        self.as_level(3)
        radios, html = self.radios(self.client.get(reverse('orders:checkout')))
        self.assertEqual(radios, [])
        self.assertIn('type="hidden" name="payment_method" value="vip"', html)

    def test_a_vip_under_each_policy(self):
        self.as_level(3)
        set_policy(VIP_CHEQUE_VIP_PRICE)
        radios, _ = self.radios(self.client.get(reverse('orders:checkout')))
        self.assertEqual(radios, ['vip', 'vip_check'])
        set_policy(VIP_CHEQUE_REQUEST)
        radios, html = self.radios(self.client.get(reverse('orders:checkout')))
        self.assertEqual(radios, ['vip'])
        self.assertIn('request-check-link', html)

    def test_the_request_page_is_a_login_protected_placeholder(self):
        response = self.client.get(reverse('accounts:soon_check_request'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'درخواست خرید چکی')


class SubmitOrderOptionTests(CheckoutTestBase):
    def setUp(self):
        super().setUp()
        set_policy(VIP_CHEQUE_DISABLED)
        self.addCleanup(cache.delete, SiteSettings.CACHE_KEY)
        self.product.price3 = 70000
        self.product.save(update_fields=['price3'])

    def as_level(self, level, flag=False):
        self.user.price_level = level
        self.user.can_purchase_with_check = flag
        self.user.save(update_fields=['price_level', 'can_purchase_with_check'])

    def submit(self, key, **extra):
        data = {'address_id': self.address.pk, 'payment_method': key}
        data.update(extra)
        with mock.patch('holoo.receivers.send_order_to_holoo'):
            with self.captureOnCommitCallbacks(execute=True):
                return self.post_order(data)

    def orders(self):
        return Order.objects.filter(user=self.user)

    # ---------- مشتری چکی ----------
    def test_the_cheque_customer_cheque_order(self):
        self.submit('check')
        order = self.orders().get()
        self.assertEqual((order.payment_method, order.settlement), ('check', 'cheque'))
        self.assertTrue(order.is_cheque)
        self.assertFalse(order.can_pay)
        self.assertEqual(order.items.get().price, 100000)

    def test_the_cheque_customer_cash_order_is_online(self):
        self.submit('cash')
        order = self.orders().get()
        self.assertEqual((order.payment_method, order.settlement), ('cash', 'online'))
        self.assertFalse(order.is_cheque)
        self.assertTrue(order.can_pay)
        self.assertEqual(order.items.get().price, 90000)
        self.assertIsNotNone(hold_expiry(order))                          # رزرو ۲۰ دقیقه‌ای فقط برای آنلاین

    # ---------- مشتری نقدی ----------
    def test_the_cash_customer_cannot_place_a_cheque_order_even_by_posting_it(self):
        self.as_level(2)
        before = CartItem.objects.count()
        response = self.submit('check', expected_total=None)
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'این روش پرداخت برای حساب شما فعال نیست', status_code=400)
        self.assertFalse(self.orders().exists())
        self.assertEqual(CartItem.objects.count(), before)                # سبد دست‌نخورده

    def test_the_cash_customer_cash_order_works(self):
        self.as_level(2)
        self.submit('cash')
        order = self.orders().get()
        self.assertEqual((order.payment_method, order.settlement, order.items.get().price), ('cash', 'online', 90000))

    def test_requesting_a_cheque_never_creates_an_order_and_keeps_the_cart(self):
        self.as_level(2)
        before = CartItem.objects.count()
        response = self.submit('request_check', expected_total=None)
        self.assertRedirects(response, reverse('accounts:soon_check_request'), fetch_redirect_response=False)
        self.assertFalse(self.orders().exists())
        self.assertEqual(CartItem.objects.count(), before)
        self.assertTrue(CartItem.objects.filter(cart=self.cart).exists())

    def test_the_permitted_cash_customer_cheque_order_uses_the_cheque_price(self):
        self.as_level(2, flag=True)
        self.submit('check')
        order = self.orders().get()
        self.assertEqual((order.payment_method, order.settlement, order.items.get().price), ('check', 'cheque', 100000))

    # ---------- مشتری ویژه ----------
    def test_a_plain_vip_cannot_buy_by_cheque_by_any_key(self):
        self.as_level(3)
        for key in ('check', 'vip_check'):
            with self.subTest(key=key):
                self.assertEqual(self.submit(key, expected_total=None).status_code, 400)
        self.assertFalse(self.orders().exists())
        self.submit('vip')
        order = self.orders().get()
        self.assertEqual((order.payment_method, order.settlement, order.items.get().price), ('vip', 'online', 70000))

    def test_vip_direct_cheque_keeps_the_vip_price(self):
        self.as_level(3)
        set_policy(VIP_CHEQUE_VIP_PRICE)
        self.submit('vip_check')
        order = self.orders().get()
        self.assertEqual((order.payment_method, order.settlement), ('vip', 'cheque'))
        self.assertEqual(order.items.get().price, 70000)
        self.assertTrue(order.is_cheque)
        self.assertFalse(order.can_pay)
        self.assertTrue(order.settled_off_site)
        self.assertIsNone(hold_expiry(order))                              # رزرو تا تصمیم مدیر (مثل هر سفارش چکی)
        self.assertEqual(order.customer_status, 'awaiting_cheque')           # تا ثبت اطلاعات چک (فاز C)
        self.assertIn('تسویه چکی', order.payment_method_title)

    def test_vip_direct_cheque_at_the_standard_price(self):
        self.as_level(3)
        set_policy(VIP_CHEQUE_STANDARD_PRICE)
        self.submit('check')
        order = self.orders().get()
        self.assertEqual((order.payment_method, order.settlement, order.items.get().price), ('check', 'cheque', 100000))

    def test_the_vip_permission_overrides_a_disabled_policy_and_keeps_the_vip_price(self):
        self.as_level(3, flag=True)
        self.submit('vip_check')
        order = self.orders().get()
        self.assertEqual((order.payment_method, order.settlement, order.items.get().price), ('vip', 'cheque', 70000))

    def test_the_vip_request_policy_redirects_without_an_order(self):
        self.as_level(3)
        set_policy(VIP_CHEQUE_REQUEST)
        response = self.submit('request_check', expected_total=None)
        self.assertRedirects(response, reverse('accounts:soon_check_request'), fetch_redirect_response=False)
        self.assertFalse(self.orders().exists())

    # ---------- پرداخت آنلاین برای سفارش چکی بسته است ----------
    def test_the_payment_view_blocks_every_cheque_order_including_vip_cheque(self):
        self.as_level(3, flag=True)
        self.submit('vip_check')
        order = self.orders().get()
        for method in ('get', 'post'):
            response = getattr(self.client, method)(reverse('payments:start_payment', args=[order.id]))
            self.assertRedirects(
                response, f"{reverse('orders:order_detail_full', args=[order.id])}?payment_blocked_reason=cheque",
                fetch_redirect_response=False)
        self.assertFalse(Transaction.objects.filter(order=order).exists())

    def test_the_order_detail_describes_a_cheque_settlement_not_a_pending_payment(self):
        self.submit('check')
        order = self.orders().get()
        response = self.client.get(reverse('orders:order_detail_full', args=[order.id]))
        self.assertContains(response, 'تسویه چکی (خارج از سایت)')

    def test_promotion_basis_is_the_effective_price_column(self):
        """ ویژه‌ای که با قیمت مصوب چکی می‌خرد سیاست «چکی» را می‌گیرد، نه «ویژه» """
        self.as_level(3)
        make_promotion(self.product, percent=20)
        policy = DiscountPolicy.load()
        policy.apply_to_vip, policy.apply_for_check, policy.apply_for_cash = False, True, False
        policy.save()
        self.assertEqual(final_price(self.product, self.user, VIP), 70000)           # سیاست ویژه خاموش
        set_policy(VIP_CHEQUE_STANDARD_PRICE)
        self.assertEqual(final_price(self.product, self.user, CHECK), 80000)         # 100000 منهای ۲۰٪ با سیاست چکی


_PHONES = iter(range(90000, 99999))


class OrderModelInvariantTests(TestCase):
    def make(self, **fields):
        phone = f'091211{next(_PHONES)}'
        user = make_approved_user(phone)
        data = dict(user=user, first_name='علی', last_name='رضایی', phone=phone, address='تهران', total_price=1000)
        data.update(fields)
        return Order.objects.create(**data)

    def test_the_cheque_price_column_always_means_cheque_settlement(self):
        order = self.make(payment_method='check')
        order.refresh_from_db()
        self.assertEqual(order.settlement, 'cheque')

    def test_an_old_style_writer_who_forgets_settlement_is_still_safe(self):
        order = self.make(payment_method='cash')
        order.payment_method = 'check'
        order.save(update_fields=['payment_method'])
        order.refresh_from_db()
        self.assertEqual(order.settlement, 'cheque')                       # update_fields هم settlement را می‌نویسد
        self.assertTrue(order.is_cheque)
        self.assertFalse(order.can_pay)

    def test_other_price_columns_stay_online_by_default(self):
        for method in ('cash', 'vip'):
            order = self.make(payment_method=method)
            self.assertEqual((order.settlement, order.is_cheque, order.can_pay), ('online', False, True))

    def test_vip_with_cheque_settlement_is_a_cheque_order(self):
        order = self.make(payment_method='vip', settlement='cheque')
        order.refresh_from_db()
        self.assertEqual((order.payment_method, order.settlement, order.is_cheque), ('vip', 'cheque', True))
        self.assertFalse(order.can_pay)

    def test_the_is_cheque_safety_net_covers_a_row_written_by_old_code(self):
        order = self.make(payment_method='cash')
        Order.objects.filter(pk=order.pk).update(payment_method='check', settlement='online')     # مثل کدی که settlement را نمی‌شناسد
        order.refresh_from_db()
        self.assertTrue(order.is_cheque)


class HolooNoteTests(TestCase):
    def test_the_invoice_note_names_the_price_column_and_the_cheque_settlement(self):
        from holoo.invoice import method_note
        make = OrderModelInvariantTests.make
        self.assertEqual(method_note(make(self, payment_method='cash')), 'ثبت از سایت - روش cash')
        self.assertEqual(method_note(make(self, payment_method='check')), 'ثبت از سایت - روش check')
        self.assertEqual(method_note(make(self, payment_method='vip', settlement='cheque')), 'ثبت از سایت - روش vip - تسویه چکی')


class SettlementMigrationTests(TestCase):
    def test_the_data_migration_marks_only_cheque_price_orders_and_is_idempotent(self):
        migration = importlib.import_module('orders.migrations.0017_cheque_settlement_separation')
        make = OrderModelInvariantTests.make
        cheque, cash, vip = (make(self, payment_method=m) for m in ('check', 'cash', 'vip'))
        Order.objects.filter(pk=cheque.pk).update(settlement='online')            # حالت پیش از مایگریشن
        migration.mark_existing_cheque_orders(django_apps, None)
        migration.mark_existing_cheque_orders(django_apps, None)                  # دوباره: بی‌اثر
        values = dict(Order.objects.values_list('payment_method', 'settlement'))
        self.assertEqual(values, {'check': 'cheque', 'cash': 'online', 'vip': 'online'})
        for order in (cheque, cash, vip):                                          # قیمت‌ها و مبلغ‌ها دست‌نخورده
            order.refresh_from_db()
            self.assertEqual(order.total_price, 1000)


class UserFlagTests(TestCase):
    def test_the_permission_defaults_off_and_never_applies_to_a_guest(self):
        from products.pricing import has_cheque_permission
        from django.contrib.auth.models import AnonymousUser
        user = make_approved_user('09121110002')
        self.assertFalse(user.can_purchase_with_check)
        self.assertFalse(has_cheque_permission(user))
        user.can_purchase_with_check = True
        self.assertTrue(has_cheque_permission(user))
        self.assertFalse(has_cheque_permission(AnonymousUser()))
        self.assertFalse(has_cheque_permission(None))

    def test_the_admin_user_form_exposes_the_permission(self):
        from django.contrib.admin.sites import site
        from accounts.models import CustomUser
        model_admin = site._registry[CustomUser]
        flat = [name for _, options in model_admin.fieldsets for name in options['fields']]
        self.assertIn('can_purchase_with_check', flat)
        self.assertIn('can_purchase_with_check', model_admin.list_filter)
