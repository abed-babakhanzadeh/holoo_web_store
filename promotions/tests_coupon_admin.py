"""
تست ادمین کدهای تخفیف، گزارش مصرف، قاعده‌های ارسال رایگان و بخش «کدهای تخفیف» در سیاست سراسری.
"""

import csv
import io
import itertools
from datetime import timedelta

import jdatetime
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomUser
from locations.models import City, Province
from orders.models import Order
from products.models import Category, Product

from .free_shipping import get_rules
from .models import Coupon, CouponRedemption, DiscountPolicy, FreeShippingRule, UserCoupon
from .testing import PromotionTestMixin, make_coupon, make_free_shipping_rule

_seq = itertools.count(1)


def jalali_fields(prefix, dt):
    jd = jdatetime.datetime.fromgregorian(datetime=timezone.localtime(dt))
    return {f'{prefix}_0': jd.strftime('%Y/%m/%d'), f'{prefix}_1': jd.strftime('%H:%M')}


class AdminBase(PromotionTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        DiscountPolicy.load()
        self.admin_user = CustomUser.objects.create_superuser(phone_number=f'0912012{next(_seq):04d}')
        self.client.force_login(self.admin_user)
        self.category = Category.objects.create(name='ادمین کوپن', slug=f'coupon-admin-cat-{next(_seq)}')
        n = next(_seq)
        self.product = Product.objects.create(name='کالای ادمین', slug=f'coupon-admin-p-{n}', erp_code=f'ERP-CA-{n}',
                                              category=self.category, price=100000, stock=5)

    def user(self):
        return CustomUser.objects.create_user(phone_number=f'0912013{next(_seq):04d}')

    def order(self, user=None):
        return Order.objects.create(user=user or self.user(), first_name='الف', last_name='ب', phone='09120000000', address='x', total_price=1000)


class CouponAdminFormTests(AdminBase):
    def form(self, **overrides):
        data = {
            'code': 'new-year', 'title': 'عیدی', 'description': '', 'is_active': 'on', 'kind': 'percent', 'value': '15',
            'max_discount_amount': '50000', 'scope': 'cart', 'min_cart_amount': '100000', 'allow_with_promotions': '',
            'total_limit': '100', 'per_user_limit': '1', 'first_order_only': '', 'audience': 'everyone',
            'min_loyalty_level': '0', 'is_claimable': '', 'claim_limit': '', 'terms': '',
            'assignments-TOTAL_FORMS': '0', 'assignments-INITIAL_FORMS': '0', 'assignments-MIN_NUM_FORMS': '0', 'assignments-MAX_NUM_FORMS': '1000',
            'redemptions-TOTAL_FORMS': '0', 'redemptions-INITIAL_FORMS': '0', 'redemptions-MIN_NUM_FORMS': '0', 'redemptions-MAX_NUM_FORMS': '0',
        }
        data.update(overrides)
        return {k: v for k, v in data.items() if v is not None}

    def add(self, **overrides):
        return self.client.post(reverse('admin:promotions_coupon_add'), self.form(**overrides))

    def test_create_with_no_dates_normalises_the_code(self):
        response = self.add()
        self.assertEqual(response.status_code, 302, getattr(response, 'context', None) and response.context['adminform'].form.errors)
        coupon = Coupon.objects.get()
        self.assertEqual((coupon.code, coupon.kind, coupon.value, coupon.max_discount_amount, coupon.min_cart_amount),
                         ('NEW-YEAR', 'percent', 15, 50000, 100000))
        self.assertEqual((coupon.starts_at, coupon.ends_at, coupon.total_limit, coupon.per_user_limit), (None, None, 100, 1))
        self.assertFalse(coupon.allow_with_promotions)

    def test_blank_code_is_generated(self):
        self.assertEqual(self.add(code='').status_code, 302)
        self.assertRegex(Coupon.objects.get().code, r'^[A-Z0-9]{8}$')

    def test_jalali_dates_are_stored_as_the_right_instants(self):
        start, end = timezone.now() - timedelta(days=1), timezone.now() + timedelta(days=9)
        data = {**jalali_fields('starts_at', start), **jalali_fields('ends_at', end)}
        self.assertEqual(self.add(**data).status_code, 302)
        coupon = Coupon.objects.get()
        self.assertLess(abs((coupon.starts_at - start).total_seconds()), 61)
        self.assertLess(abs((coupon.ends_at - end).total_seconds()), 61)

    def test_end_before_start_is_rejected(self):
        now = timezone.now()
        data = {**jalali_fields('starts_at', now + timedelta(days=2)), **jalali_fields('ends_at', now + timedelta(days=1))}
        response = self.add(**data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'پایان اعتبار باید بعد از شروع باشد')

    def test_validation_errors_are_shown(self):
        self.assertContains(self.add(value='150'), 'درصد باید بین ۱ تا ۱۰۰ باشد')
        self.assertContains(self.add(kind='fixed', value='1000', max_discount_amount='5'), 'سقف مبلغ فقط برای کد درصدی')
        self.assertContains(self.add(per_user_limit='0'), 'سقف هر کاربر باید حداقل ۱ باشد')
        self.assertEqual(Coupon.objects.count(), 0)

    def test_scope_items_needs_a_product_or_category(self):
        response = self.add(scope='items')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'حداقل یک محصول یا دسته انتخاب کنید')
        ok = self.add(scope='items', products=[self.product.pk])
        self.assertEqual(ok.status_code, 302)
        self.assertEqual(list(Coupon.objects.get().products.all()), [self.product])

    def test_categories_scope(self):
        self.assertEqual(self.add(scope='items', categories=[self.category.pk]).status_code, 302)
        self.assertEqual(list(Coupon.objects.get().categories.all()), [self.category])

    def test_free_shipping_kind_needs_the_cart_scope(self):
        response = self.add(kind='free_shipping', value='0', max_discount_amount='', scope='items', products=[self.product.pk])
        self.assertContains(response, 'شمول را «کل سبد» بگذارید')

    def test_duplicate_code_in_another_case_is_rejected_by_the_form(self):
        make_coupon('TAKEN')
        response = self.add(code='taken')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Coupon.objects.count(), 1)

    def test_assigned_audience_with_an_inline_user(self):
        user = self.user()
        data = self.form(audience='assigned', **{'assignments-TOTAL_FORMS': '1', 'assignments-0-user': str(user.pk), 'assignments-0-source': 'admin'})
        self.assertEqual(self.client.post(reverse('admin:promotions_coupon_add'), data).status_code, 302)
        self.assertEqual(UserCoupon.objects.get().user, user)

    def test_change_page_shows_the_usage_report(self):
        coupon = make_coupon('REPORT')
        user = self.user()
        for status, amount in (('redeemed', 10000), ('redeemed', 5000), ('reserved', 7000), ('released', 3000)):
            CouponRedemption.objects.create(coupon=coupon, user=user, order_id=self.order(user).pk, code='REPORT',
                                            status=status, discount_amount=amount, expires_at=timezone.now() + timedelta(minutes=5))
        page = self.client.get(reverse('admin:promotions_coupon_change', args=[coupon.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'مصرف‌شده: <b>2</b> (15,000 تومان تخفیف)')
        self.assertContains(page, 'رزرو‌شده (در انتظار پرداخت): <b>1</b>')
        self.assertContains(page, 'آزادشده: <b>1</b>')

    def test_redemptions_inline_is_read_only(self):
        coupon = make_coupon('RO')
        page = self.client.get(reverse('admin:promotions_coupon_change', args=[coupon.pk]))
        self.assertNotContains(page, 'name="redemptions-0-')


class CouponAdminListTests(AdminBase):
    def setUp(self):
        super().setUp()
        self.active = make_coupon('ACTIVE1', title='فعال')
        self.expired = make_coupon('EXPIRED1', title='منقضی', expired=True)
        self.scheduled = make_coupon('SCHED1', title='آینده', scheduled=True)
        self.inactive = make_coupon('OFF1', title='غیرفعال', active=False)
        self.url = reverse('admin:promotions_coupon_changelist')

    def ids_in(self, **params):
        body = self.client.get(self.url, params).content.decode()
        return {c.code for c in Coupon.objects.all() if f'/{c.pk}/change/' in body}

    def test_status_filter_including_open_ended_windows(self):
        half_open = make_coupon('HALFOPEN', ends_at=timezone.now() + timedelta(days=1))            # بدون شروع
        self.assertEqual(self.ids_in(status='active'), {'ACTIVE1', 'HALFOPEN'})
        self.assertEqual(self.ids_in(status='expired'), {'EXPIRED1'})
        self.assertEqual(self.ids_in(status='scheduled'), {'SCHED1'})
        self.assertEqual(self.ids_in(status='inactive'), {'OFF1'})
        self.assertIsNotNone(half_open)

    def test_other_filters_and_search(self):
        make_coupon('SHIPONLY', kind='free_shipping')
        make_coupon('NEWBIE', first_order_only=True)
        self.assertEqual(self.ids_in(kind='free_shipping'), {'SHIPONLY'})
        self.assertEqual(self.ids_in(first_order_only__exact='1'), {'NEWBIE'})
        self.assertEqual(self.ids_in(q='newb'), {'NEWBIE'})
        self.assertEqual(self.ids_in(q='منقضی'), {'EXPIRED1'})

    def test_list_shows_usage_and_the_discount_given(self):
        coupon = make_coupon('USED', total_limit=10)
        user = self.user()
        for status, amount in (('redeemed', 20000), ('reserved', 9999), ('redeemed', 5000)):
            CouponRedemption.objects.create(coupon=coupon, user=user, order_id=self.order(user).pk, code='USED', status=status,
                                            discount_amount=amount, expires_at=timezone.now() + timedelta(minutes=5))
        CouponRedemption.objects.create(coupon=coupon, user=user, order_id=self.order(user).pk, code='USED', status='reserved',
                                        discount_amount=1, expires_at=timezone.now() - timedelta(minutes=5))     # منقضی؛ نمی‌شمارد
        body = self.client.get(self.url, {'q': 'USED'}).content.decode()
        self.assertIn('3 از 10', body)
        self.assertIn('25,000 تومان', body)

    def test_unlimited_shows_infinity(self):
        self.assertIn('0 از ∞', self.client.get(self.url, {'q': 'ACTIVE1'}).content.decode())

    def test_activate_deactivate_and_extend(self):
        self.client.post(self.url, {'action': 'activate', '_selected_action': [self.inactive.pk]})
        self.inactive.refresh_from_db()
        self.assertTrue(self.inactive.is_active)
        self.client.post(self.url, {'action': 'deactivate', '_selected_action': [self.active.pk]})
        self.active.refresh_from_db()
        self.assertFalse(self.active.is_active)
        old_end = self.scheduled.ends_at
        self.client.post(self.url, {'action': 'extend_7_days', '_selected_action': [self.scheduled.pk, self.active.pk]})
        self.scheduled.refresh_from_db()
        self.assertEqual(self.scheduled.ends_at, old_end + timedelta(days=7))
        self.active.refresh_from_db()
        self.assertIsNone(self.active.ends_at)                                       # بدون پایان دست نمی‌خورد

    def test_csv_export(self):
        user = self.user()
        CouponRedemption.objects.create(coupon=self.active, user=user, order_id=self.order(user).pk, code='ACTIVE1',
                                        status='redeemed', discount_amount=100)
        response = self.client.post(self.url, {'action': 'export_csv', '_selected_action': [self.active.pk, self.expired.pk]})
        self.assertEqual(response.status_code, 200)
        self.assertIn('text/csv', response['Content-Type'])
        text = response.content.decode('utf-8')
        self.assertTrue(text.startswith('﻿'))                                       # BOM برای اکسل
        rows = list(csv.reader(io.StringIO(text.lstrip('﻿'))))
        self.assertEqual(rows[0][:4], ['code', 'title', 'kind', 'value'])
        by_code = {row[0]: row for row in rows[1:]}
        self.assertEqual(set(by_code), {'ACTIVE1', 'EXPIRED1'})
        self.assertEqual((by_code['ACTIVE1'][4], by_code['ACTIVE1'][5]), ('active', '1'))
        self.assertEqual(by_code['EXPIRED1'][4], 'expired')


class BulkGenerateTests(AdminBase):
    def setUp(self):
        super().setUp()
        self.template = make_coupon('TEMPLATE', value=25, max_discount_amount=30000, min_cart_amount=150000, per_user_limit=1,
                                    total_limit=None, first_order_only=True, products=[self.product], title='الگوی عیدی',
                                    ends_at=timezone.now() + timedelta(days=30))
        self.url = reverse('admin:promotions_coupon_changelist')

    def generate(self, selected=None, **fields):
        data = {'action': 'bulk_generate', '_selected_action': selected or [self.template.pk], 'apply': '1',
                'prefix': 'yalda', 'count': '50', 'length': '8', 'total_limit': '1'}
        data.update(fields)
        return self.client.post(self.url, data)

    def test_intermediate_page(self):
        response = self.client.post(self.url, {'action': 'bulk_generate', '_selected_action': [self.template.pk]})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'TEMPLATE')
        self.assertContains(response, 'name="prefix"')
        self.assertContains(response, 'name="count"')

    def test_creates_unique_prefixed_codes_that_copy_the_template(self):
        response = self.generate()
        self.assertEqual(response.status_code, 302)
        created = Coupon.objects.filter(code__startswith='YALDA-')
        self.assertEqual(created.count(), 50)
        self.assertEqual(len({c.code for c in created}), 50)
        for coupon in created[:5]:
            self.assertRegex(coupon.code, r'^YALDA-[A-HJKMNP-Z2-9]{8}$')
            self.assertEqual((coupon.kind, coupon.value, coupon.max_discount_amount, coupon.min_cart_amount, coupon.first_order_only),
                             ('percent', 25, 30000, 150000, True))
            self.assertEqual((coupon.total_limit, coupon.per_user_limit, coupon.scope, coupon.title), (1, 1, 'items', 'الگوی عیدی'))
            self.assertEqual(coupon.ends_at, self.template.ends_at)
            self.assertEqual(list(coupon.products.all()), [self.product])
        self.assertEqual(Coupon.objects.count(), 51)                                     # الگو هم مانده

    def test_blank_total_limit_inherits_the_template(self):
        self.generate(total_limit='', count='3', prefix='')
        others = Coupon.objects.exclude(pk=self.template.pk)
        self.assertEqual(others.count(), 3)
        self.assertEqual({c.total_limit for c in others}, {None})
        self.assertTrue(all(len(c.code) == 8 for c in others))

    def test_category_scope_is_copied_too(self):
        template = make_coupon('CATTPL', categories=[self.category])
        self.generate(selected=[template.pk], count='4', prefix='cat')
        for coupon in Coupon.objects.filter(code__startswith='CAT-'):
            self.assertEqual(list(coupon.categories.all()), [self.category])

    def test_requires_exactly_one_template(self):
        other = make_coupon('OTHER')
        response = self.client.post(self.url, {'action': 'bulk_generate', '_selected_action': [self.template.pk, other.pk]}, follow=True)
        self.assertContains(response, 'دقیقاً یک کد را به‌عنوان الگو انتخاب کنید')
        self.assertEqual(Coupon.objects.count(), 2)

    def test_form_validation(self):
        for bad in ({'count': '0'}, {'count': '5001'}, {'length': '5'}, {'prefix': 'بد!'}, {'prefix': 'a b!'}):
            with self.subTest(bad=bad):
                response = self.generate(**bad)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(Coupon.objects.count(), 1)

    def test_generated_codes_never_collide_with_existing_ones(self):
        Coupon.objects.create(code='DUP-AAAAAAAA', title='t', kind='percent', value=5)
        self.generate(prefix='dup', count='30')
        self.assertEqual(Coupon.objects.filter(code__startswith='DUP-').count(), 31)

    def test_bulk_generated_codes_work_at_checkout_evaluation(self):
        self.generate(count='2', prefix='ok', total_limit='1')
        from . import coupons
        for coupon in Coupon.objects.filter(code__startswith='OK-'):
            self.assertEqual(coupons.find_coupon(coupon.code.lower()), coupon)


class RedemptionAdminTests(AdminBase):
    def setUp(self):
        super().setUp()
        self.coupon = make_coupon('REDEEMADMIN')
        self.user_ = self.user()
        self.order_ = self.order(self.user_)
        self.redemption = CouponRedemption.objects.create(coupon=self.coupon, user=self.user_, order_id=self.order_.pk, code='REDEEMADMIN',
                                                          status='reserved', discount_amount=1234, expires_at=timezone.now() + timedelta(minutes=9))

    def test_list_links_to_the_order_and_filters(self):
        url = reverse('admin:promotions_couponredemption_changelist')
        body = self.client.get(url).content.decode()
        self.assertIn(reverse('admin:orders_order_change', args=[self.order_.pk]), body)
        self.assertIn('1234', body)
        self.assertIn('REDEEMADMIN', self.client.get(url, {'status': 'reserved'}).content.decode())
        self.assertNotIn('REDEEMADMIN', self.client.get(url, {'status': 'released'}).content.decode())
        self.assertIn('REDEEMADMIN', self.client.get(url, {'q': str(self.order_.pk)}).content.decode())

    def test_read_only_no_add_change_or_delete(self):
        self.assertEqual(self.client.get(reverse('admin:promotions_couponredemption_add')).status_code, 403)
        page = self.client.get(reverse('admin:promotions_couponredemption_change', args=[self.redemption.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'name="status"')
        self.assertNotContains(page, 'name="_save"')
        self.assertEqual(self.client.post(reverse('admin:promotions_couponredemption_delete', args=[self.redemption.pk]), {'post': 'yes'}).status_code, 403)
        self.assertTrue(CouponRedemption.objects.filter(pk=self.redemption.pk).exists())

    def test_a_coupon_with_redemptions_cannot_be_deleted(self):
        from django.db.models import ProtectedError
        with self.assertRaises(ProtectedError):
            self.coupon.delete()


class FreeShippingRuleAdminTests(AdminBase):
    def setUp(self):
        super().setUp()
        self.province = Province.objects.create(name='استان ادمین ارسال')
        self.city = City.objects.create(province=self.province, name='شهر ادمین ارسال')

    def form(self, **overrides):
        data = {'title': 'کمپین نوروز', 'is_active': 'on', 'priority': '3', 'min_cart_total': '2000000', 'postage_mode': 'ignore'}
        data.update(overrides)
        return {k: v for k, v in data.items() if v is not None}

    def test_create_with_scope_and_dates_and_the_cache_updates_immediately(self):
        start, end = timezone.now() - timedelta(days=1), timezone.now() + timedelta(days=5)
        data = self.form(provinces=[self.province.pk], cities=[self.city.pk], **jalali_fields('starts_at', start), **jalali_fields('ends_at', end))
        self.assertEqual(get_rules(), ())                                                 # کش گرم و خالی
        response = self.client.post(reverse('admin:promotions_freeshippingrule_add'), data)
        self.assertEqual(response.status_code, 302, getattr(response, 'context', None) and response.context['adminform'].form.errors)
        rule = FreeShippingRule.objects.get()
        self.assertEqual((rule.min_cart_total, rule.priority, rule.postage_mode), (2000000, 3, 'ignore'))
        self.assertEqual(list(rule.provinces.all()), [self.province])
        self.assertEqual(list(rule.cities.all()), [self.city])
        snapshot = get_rules()[0]                                                         # M2M بعد از save ← کش باطل شده
        self.assertEqual((snapshot.min_total, snapshot.province_ids, snapshot.city_ids),
                         (2000000, frozenset({self.province.pk}), frozenset({self.city.pk})))

    def test_end_before_start_is_rejected(self):
        now = timezone.now()
        data = self.form(**jalali_fields('starts_at', now + timedelta(days=2)), **jalali_fields('ends_at', now + timedelta(days=1)))
        response = self.client.post(reverse('admin:promotions_freeshippingrule_add'), data)
        self.assertContains(response, 'پایان باید بعد از شروع باشد')

    def test_nationwide_when_no_scope_is_given(self):
        self.client.post(reverse('admin:promotions_freeshippingrule_add'), self.form())
        self.assertTrue(get_rules()[0].nationwide)

    def test_list_summary_status_filter_and_priority_editing(self):
        make_free_shipping_rule('ملی', min_total=0)
        make_free_shipping_rule('استانی', provinces=[self.province], min_total=500000, postage_mode='cover')
        make_free_shipping_rule('منقضی', expired=True)
        make_free_shipping_rule('آینده', scheduled=True)
        url = reverse('admin:promotions_freeshippingrule_changelist')
        body = self.client.get(url).content.decode()
        self.assertIn('کل کشور', body)
        self.assertIn('استان: استان ادمین ارسال', body)
        self.assertIn('بدون حداقل', body)
        self.assertIn('500,000 تومان', body)
        titles = lambda **p: {r.title for r in FreeShippingRule.objects.all() if f'/{r.pk}/change/' in self.client.get(url, p).content.decode()}
        self.assertEqual(titles(status='expired'), {'منقضی'})
        self.assertEqual(titles(status='scheduled'), {'آینده'})
        self.assertEqual(titles(status='active'), {'ملی', 'استانی'})

    def test_deactivating_from_the_list_takes_effect_immediately(self):
        rule = make_free_shipping_rule('خاموشی')
        self.assertEqual(len(get_rules()), 1)
        rule.is_active = False
        rule.save()
        self.assertEqual(get_rules(), ())


class PolicyAdminCouponSettingsTests(AdminBase):
    def test_policy_form_has_the_coupon_settings_and_saves_them(self):
        url = reverse('admin:promotions_discountpolicy_change', args=[1])
        page = self.client.get(url)
        for name in ('coupon_reservation_minutes', 'coupon_max_invalid_attempts', 'coupon_attempt_window_minutes',
                     'free_shipping_rules_enabled', 'free_shipping_threshold_after_coupon'):
            self.assertContains(page, f'name="{name}"')
        base = {'promotions_enabled': 'on', 'apply_for_cash': 'on', 'apply_for_check': 'on', 'promotion_stacking': 'best',
                'max_item_discount_percent': '90', 'rounding_step': '1'}
        response = self.client.post(url, {**base, 'coupon_reservation_minutes': '45', 'coupon_max_invalid_attempts': '5',
                                          'coupon_attempt_window_minutes': '30', 'free_shipping_rules_enabled': 'on'})
        self.assertEqual(response.status_code, 302)
        policy = DiscountPolicy.load()
        self.assertEqual((policy.coupon_reservation_minutes, policy.coupon_max_invalid_attempts, policy.coupon_attempt_window_minutes), (45, 5, 30))
        self.assertEqual((policy.free_shipping_rules_enabled, policy.free_shipping_threshold_after_coupon), (True, False))   # تیک نخورده = خاموش

    def test_admin_can_turn_the_free_shipping_switches_off_and_on(self):
        url = reverse('admin:promotions_discountpolicy_change', args=[1])
        base = {'promotions_enabled': 'on', 'apply_for_cash': 'on', 'apply_for_check': 'on', 'promotion_stacking': 'best',
                'max_item_discount_percent': '90', 'rounding_step': '1', 'coupon_reservation_minutes': '30',
                'coupon_max_invalid_attempts': '10', 'coupon_attempt_window_minutes': '60'}
        self.client.post(url, {**base})                                                        # هر دو خاموش
        policy = DiscountPolicy.load()
        self.assertEqual((policy.free_shipping_rules_enabled, policy.free_shipping_threshold_after_coupon), (False, False))
        self.client.post(url, {**base, 'free_shipping_rules_enabled': 'on', 'free_shipping_threshold_after_coupon': 'on'})
        policy = DiscountPolicy.load()
        self.assertEqual((policy.free_shipping_rules_enabled, policy.free_shipping_threshold_after_coupon), (True, True))

    def test_out_of_range_values_are_rejected(self):
        url = reverse('admin:promotions_discountpolicy_change', args=[1])
        base = {'promotions_enabled': 'on', 'apply_for_cash': 'on', 'apply_for_check': 'on', 'promotion_stacking': 'best',
                'max_item_discount_percent': '90', 'rounding_step': '1', 'coupon_reservation_minutes': '1',
                'coupon_max_invalid_attempts': '0', 'coupon_attempt_window_minutes': '60'}
        response = self.client.post(url, base)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(DiscountPolicy.load().coupon_reservation_minutes, 30)


class OrderAdminCouponFieldsTests(AdminBase):
    def test_coupon_snapshot_is_read_only_and_searchable(self):
        order = self.order()
        Order.objects.filter(pk=order.pk).update(coupon_code='SNAP1', order_discount=500, shipping_discount=45000)
        page = self.client.get(reverse('admin:orders_order_change', args=[order.pk]))
        self.assertEqual(page.status_code, 200)
        for name in ('coupon_code', 'shipping_discount'):
            self.assertNotContains(page, f'name="{name}"')
        self.assertContains(page, 'SNAP1')
        self.assertContains(self.client.get(reverse('admin:orders_order_changelist'), {'q': 'SNAP1'}), f'/{order.pk}/change/')
