"""
تست گزارش تحلیلی تخفیف‌ها (promotions/reports.py + صفحه‌ی ادمین): درستی جمع‌ها، بازه، سفارش‌های لغوشده، رتبه‌بندی‌ها،
تعداد ثابت کوئری، و دسترسی.
"""

import itertools
from datetime import timedelta

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomUser
from orders.models import Order, OrderItem
from products.models import Category, Product

from . import reports
from .models import Coupon, CouponRedemption, DiscountPolicy, UserCoupon
from .testing import PromotionTestMixin, make_coupon

_seq = itertools.count(1)


class ReportBase(PromotionTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        DiscountPolicy.load()
        self.now = timezone.now()
        self.category = Category.objects.create(name='گزارش', slug=f'report-cat-{next(_seq)}')
        self.users = [CustomUser.objects.create_user(phone_number=f'0912018{next(_seq):04d}', price_level=1) for _ in range(4)]

    def product(self, name):
        n = next(_seq)
        return Product.objects.create(name=name, slug=f'report-p-{n}', erp_code=f'ERP-RP-{n}', category=self.category, price=100000, stock=9)

    def order(self, user=None, total=100000, days_ago=1, status='pending', **fields):
        order = Order.objects.create(user=user or self.users[0], first_name='الف', last_name='ب', phone='09120000000', address='x',
                                     total_price=total, status=status, **fields)
        Order.objects.filter(pk=order.pk).update(created_at=self.now - timedelta(days=days_ago))
        return order

    def redemption(self, coupon, user=None, status='redeemed', discount=0, shipping=0, days_ago=1, order=None, expires_in=30, **fields):
        order = order or self.order(user)
        row = CouponRedemption.objects.create(
            coupon=coupon, user=user or self.users[0], order_id=order.pk, code=coupon.code, status=status, discount_amount=discount,
            shipping_discount=shipping, expires_at=self.now + timedelta(minutes=expires_in), **fields)
        CouponRedemption.objects.filter(pk=row.pk).update(reserved_at=self.now - timedelta(days=days_ago))
        return row

    def item(self, order, product, price=90000, discount=10000, quantity=1):
        return OrderItem.objects.create(order=order, product=product, price=price, original_price=price + discount,
                                        discount_amount=discount, quantity=quantity)


class ReportNumbersTests(ReportBase):
    def setUp(self):
        super().setUp()
        self.percent = make_coupon('PERCENT1', value=10, per_user_limit=None)
        self.ship = make_coupon('SHIP1', kind='free_shipping', per_user_limit=None)
        self.fixed = make_coupon('FIXED1', kind='fixed', value=5000, per_user_limit=None)
        u1, u2, u3, u4 = self.users
        # مصرف‌های نهایی
        self.redemption(self.percent, u1, discount=20000)
        self.redemption(self.percent, u2, discount=30000)
        self.redemption(self.percent, u1, discount=10000)
        self.redemption(self.ship, u3, shipping=45000)
        self.redemption(self.fixed, u4, discount=5000)
        # رزرو معتبر، رزرو منقضی، آزادشده، over_limit
        self.redemption(self.percent, u2, status='reserved', discount=7000, expires_in=10)
        self.redemption(self.ship, u3, status='reserved', shipping=50000, expires_in=10)
        self.redemption(self.percent, u3, status='reserved', discount=999, expires_in=-5)
        self.redemption(self.fixed, u4, status='released', discount=5000)
        self.redemption(self.fixed, u1, discount=1, over_limit=True)

    def report(self, days=30):
        return reports.build_report(days, now=self.now)

    def test_coupon_counters_and_sums(self):
        c = self.report()['coupons']
        self.assertEqual((c['redeemed_count'], c['waiting_count'], c['released_count']), (6, 2, 2))    # ۶ نهایی، ۲ رزروِ معتبر، ۱ آزاد + ۱ رزروِ منقضی
        self.assertEqual((c['goods_discount'], c['shipping_total'], c['total_discount']), (65001, 45000, 110001))
        self.assertEqual((c['waiting_discount'], c['waiting_shipping']), (57000, 50000))
        self.assertEqual(c['users'], 4)
        self.assertEqual(c['average_discount'], 110001 // 6)
        self.assertEqual(c['over_limit_count'], 1)

    def test_top_coupons_rank_by_uses_then_discount(self):
        rows = self.report()['top_coupons']
        self.assertEqual([r['code'] for r in rows], ['PERCENT1', 'SHIP1', 'FIXED1'])            # SHIP1 و FIXED1 هر دو ۲ مصرف؛ تخفیف بیشتر جلوتر
        percent = rows[0]
        self.assertEqual((percent['uses'], percent['discount'], percent['users']), (4, 67000, 2))      # ۳ نهایی + ۱ رزروِ معتبر
        self.assertEqual((rows[1]['uses'], int(rows[1]['discount'])), (2, 95000))                        # ship: ۱ نهایی + ۱ رزروِ معتبر
        self.assertEqual((rows[2]['uses'], int(rows[2]['discount'])), (2, 5001))                         # fixed: ۱ نهایی + over_limit نهایی

    def test_top_by_discount_uses_only_final_redemptions(self):
        rows = self.report()['top_by_discount']
        self.assertEqual([(r['code'], int(r['discount'])) for r in rows], [('PERCENT1', 60000), ('SHIP1', 45000), ('FIXED1', 5001)])

    def test_breakdown_by_kind(self):
        rows = {r['kind']: r for r in self.report()['by_kind']}
        self.assertEqual(rows['درصدی (با سقف مبلغ)']['uses'], 3)
        self.assertEqual(int(rows['ارسال رایگان (پیک درون‌شهری)']['discount']), 45000)
        self.assertEqual(int(rows['مبلغ ثابت']['discount']), 5001)

    def test_period_filters_by_redemption_date(self):
        old = self.redemption(self.percent, self.users[0], discount=999999, days_ago=100)
        self.assertEqual(self.report(30)['coupons']['goods_discount'], 65001)
        self.assertEqual(self.report(90)['coupons']['goods_discount'], 65001)
        self.assertEqual(self.report(365)['coupons']['goods_discount'], 65001 + 999999)
        self.assertEqual(self.report(0)['coupons']['goods_discount'], 65001 + 999999)
        self.assertEqual(self.report(7)['coupons']['redeemed_count'], 6)
        old.delete()

    def test_daily_series_is_grouped_per_day(self):
        self.redemption(self.percent, self.users[1], discount=100, days_ago=3)
        self.redemption(self.percent, self.users[1], discount=200, days_ago=3)
        rows = {r['day']: r for r in self.report()['daily']}
        three_days_ago = (self.now - timedelta(days=3)).date()
        self.assertEqual((rows[three_days_ago]['uses'], int(rows[three_days_ago]['discount'])), (2, 300))
        self.assertTrue(all(r['day'] >= (self.now - timedelta(days=14)).date() for r in rows.values()))

    def test_empty_database_gives_zeros_not_errors(self):
        CouponRedemption.objects.all().delete()
        Order.objects.all().delete()
        Coupon.objects.all().delete()
        result = self.report()
        self.assertEqual((result['coupons']['redeemed_count'], result['coupons']['total_discount'], result['coupons']['average_discount']), (0, 0, 0))
        self.assertEqual((result['orders']['orders'], result['orders']['coupon_share'], result['orders']['promotion_share']), (0, 0, 0))
        self.assertEqual((result['top_coupons'], result['top_products'], result['by_kind'], result['daily']), ([], [], [], []))


class ReportOrdersTests(ReportBase):
    def test_orders_promotions_and_free_shipping(self):
        p1, p2 = self.product('کالای الف'), self.product('کالای ب')
        o1 = self.order(total=200000, promotion_discount=30000)
        self.item(o1, p1, price=90000, discount=10000, quantity=3)
        o2 = self.order(total=100000, promotion_discount=5000, coupon_code='CODE1', order_discount=8000)
        self.item(o2, p1, price=95000, discount=5000, quantity=1)
        self.item(o2, p2, price=50000, discount=0, quantity=2)                                        # بدون تخفیف ← در پرتخفیف‌ها نمی‌آید
        o3 = self.order(total=50000, shipping_discount=45000)
        o4 = self.order(total=70000)
        canceled = self.order(total=999999, status='canceled', promotion_discount=99999, coupon_code='X', shipping_discount=1)
        self.item(canceled, p2, price=1, discount=1234, quantity=9)
        old = self.order(total=555, days_ago=200, promotion_discount=777)
        r = reports.build_report(30, now=self.now)
        o = r['orders']
        self.assertEqual((o['orders'], o['with_coupon'], o['with_promotion'], o['with_free_shipping']), (4, 1, 2, 1))
        self.assertEqual((int(o['promotion_total']), int(o['coupon_goods_discount']), int(o['shipping_waived']), int(o['revenue'])),
                         (35000, 8000, 45000, 420000))
        self.assertEqual((o['coupon_share'], o['promotion_share']), (25.0, 50.0))
        products = {row['product__name']: row for row in r['top_products']}
        self.assertEqual(set(products), {'کالای الف'})                                                # لغوشده و بدون تخفیف حذف
        self.assertEqual((int(products['کالای الف']['discount']), products['کالای الف']['units'], products['کالای الف']['orders']), (35000, 4, 2))
        self.assertEqual(reports.build_report(0, now=self.now)['orders']['orders'], 5)                # از ابتدا: سفارش قدیمی هم می‌آید (لغوشده نه)
        self.assertIsNotNone(o3)
        self.assertIsNotNone(o4)
        self.assertIsNotNone(old)

    def test_shipping_waiver_is_split_between_coupons_and_rules(self):
        ship = make_coupon('SHIPSPLIT', kind='free_shipping', per_user_limit=None)
        by_coupon = self.order(total=100000, shipping_discount=45000, coupon_code='SHIPSPLIT')
        self.redemption(ship, self.users[0], shipping=45000, order=by_coupon)
        self.order(total=100000, shipping_discount=60000)                                             # قاعده‌ی ارسال رایگان
        o = reports.build_report(30, now=self.now)['orders']
        self.assertEqual((int(o['shipping_waived']), int(o['shipping_waived_by_coupon']), int(o['shipping_waived_by_rule'])), (105000, 45000, 60000))

    def test_claims_are_counted_in_the_inventory(self):
        c1 = make_coupon('CLAIMED1', is_claimable=True, title='عنوان')
        make_coupon('OFFCLAIM', is_claimable=True, active=False, title='عنوان ۲')
        UserCoupon.objects.create(coupon=c1, user=self.users[0], source='claimed')
        UserCoupon.objects.create(coupon=c1, user=self.users[1], source='admin')
        inv = reports.build_report(30, now=self.now)['inventory']
        self.assertEqual((inv['claims_in_period'], inv['claimable'], inv['total'], inv['active']), (1, 1, 2, 1))

    def test_normalize_days(self):
        self.assertEqual([reports.normalize_days(x) for x in ('7', '30', '90', '365', '0', 'abc', '5', None, '-3', '')], [7, 30, 90, 365, 0, 30, 30, 30, 30, 30])


class ReportCostTests(ReportBase):
    def count(self):
        with CaptureQueriesContext(connection) as queries:
            reports.build_report(30, now=self.now)
        return len(queries)

    def test_query_count_is_small_and_independent_of_the_data_size(self):
        coupon = make_coupon('COST1', per_user_limit=None)
        product = self.product('کالا')
        for i in range(5):
            order = self.order(total=1000 + i, promotion_discount=100)
            self.item(order, product)
            self.redemption(coupon, self.users[i % 4], discount=10, order=order)
        few = self.count()
        for i in range(60):
            order = self.order(total=1000 + i, promotion_discount=100, coupon_code='COST1')
            self.item(order, product)
            self.redemption(coupon, self.users[i % 4], discount=10, order=order)
        self.assertEqual(self.count(), few)
        self.assertLessEqual(few, 10)

    def test_a_few_thousand_rows_aggregate_quickly(self):
        import time
        coupon = make_coupon('BIGDATA', per_user_limit=None)
        orders = Order.objects.bulk_create([
            Order(user=self.users[i % 4], first_name='ا', last_name='ب', phone='09120000000', address='x', total_price=1000,
                  promotion_discount=100) for i in range(1500)], batch_size=100)
        ids = list(Order.objects.order_by('-id').values_list('id', flat=True)[:1500])
        CouponRedemption.objects.bulk_create([
            CouponRedemption(coupon=coupon, user=self.users[i % 4], order_id=oid, code='BIGDATA', status='redeemed', discount_amount=10,
                             expires_at=self.now) for i, oid in enumerate(ids)], batch_size=100)
        started = time.perf_counter()
        result = reports.build_report(0, now=self.now + timedelta(minutes=1))
        elapsed = time.perf_counter() - started
        self.assertEqual(result['coupons']['redeemed_count'], 1500)
        self.assertEqual(int(result['coupons']['goods_discount']), 15000)
        self.assertLess(elapsed, 3.0)
        self.assertEqual(len(orders), 1500)


class ReportAdminTests(ReportBase):
    def setUp(self):
        super().setUp()
        self.admin_user = CustomUser.objects.create_superuser(phone_number=f'0912019{next(_seq):04d}')
        self.client.force_login(self.admin_user)
        self.url = reverse('admin:promotions_coupon_report')

    def test_page_renders_the_numbers(self):
        coupon = make_coupon('ADMINREP', per_user_limit=None, title='عنوان گزارش')
        self.redemption(coupon, self.users[0], discount=12345)
        self.redemption(coupon, self.users[1], discount=7000)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        for anchor in ('id="kpi-coupons"', 'id="top-coupons"', 'id="top-discount"', 'id="by-kind"', 'id="daily"', 'id="kpi-orders"', 'id="top-products"'):
            self.assertIn(anchor, html)
        self.assertIn('ADMINREP', html)
        self.assertIn('عنوان گزارش', html)
        self.assertRegex(html, r'19[,٬]?345')                                                          # جمع تخفیف (گروه‌بندی هزارگان مجاز)
        self.assertEqual(response.context['days'], 30)

    def test_period_links_and_selection(self):
        html = self.client.get(self.url, {'days': '90'}).content.decode()
        for days in ('?days=7', '?days=30', '?days=90', '?days=365', '?days=0'):
            self.assertIn(days, html)
        self.assertRegex(html, r'href="\?days=90" class="on"')
        self.assertEqual(self.client.get(self.url, {'days': 'evil'}).context['days'], 30)
        self.assertEqual(self.client.get(self.url, {'days': '0'}).context['days'], 0)

    def test_empty_state(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn('داده‌ای در این بازه نیست', html)

    def test_link_on_the_coupon_changelist(self):
        self.assertContains(self.client.get(reverse('admin:promotions_coupon_changelist')), self.url)

    def test_access_control(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)                                    # ورود لازم است
        plain = CustomUser.objects.create_user(phone_number=f'0912019{next(_seq):04d}')
        self.client.force_login(plain)
        self.assertIn(self.client.get(self.url).status_code, (302, 403))
        staff = CustomUser.objects.create_user(phone_number=f'0912019{next(_seq):04d}', is_staff=True)   # کارمند بدون مجوز مشاهده‌ی کوپن
        self.client.force_login(staff)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_a_staff_with_view_permission_can_open_it(self):
        from django.contrib.auth.models import Permission
        staff = CustomUser.objects.create_user(phone_number=f'0912019{next(_seq):04d}', is_staff=True)
        staff.user_permissions.add(Permission.objects.get(codename='view_coupon'))
        self.client.force_login(staff)
        self.assertEqual(self.client.get(self.url).status_code, 200)
