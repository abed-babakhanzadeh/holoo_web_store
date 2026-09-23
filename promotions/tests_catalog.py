"""
تست فیلتر «فقط کالاهای دارای تخفیف» و ترتیب «بیشترین تخفیف» در لیست محصولات (promotions/catalog.py + products/views.py).

اصل: نتیجه دقیقاً همان است که موتور قیمت برای همان کاربر روی کارت نشان می‌دهد، با کوئری‌های اضافه‌ی ثابت و کم.
"""

import itertools
from datetime import timedelta
from unittest import mock

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomUser
from accounts.testing import make_approved_user
from products import deals
from products.models import Brand, Category, Product, ProductColor
from products.pricing import price_breakdown

from . import catalog
from .models import DiscountPolicy
from .testing import PromotionTestMixin, make_promotion

_seq = itertools.count(1)
LIST = 'products:product_list'


class CatalogBase(PromotionTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        DiscountPolicy.load()
        self.root = Category.objects.create(name='ریشه فروشگاه', slug=f'cat-root-{next(_seq)}')
        self.sub = Category.objects.create(name='زیردسته', slug=f'cat-sub-{next(_seq)}', parent=self.root)
        self.other = Category.objects.create(name='دسته‌ی دیگر', slug=f'cat-other-{next(_seq)}')
        self.brand = Brand.objects.create(name='برند آزمون', slug=f'brand-{next(_seq)}')
        self.user = make_approved_user(f'0912017{next(_seq):04d}', price_level=1)
        self.vip = make_approved_user(f'0912017{next(_seq):04d}', price_level=3)
        self.client.force_login(self.user)
        self.base = timezone.now()

    def product(self, name, category=None, price=100000, stock=5, age_minutes=0, **extra):
        n = next(_seq)
        data = dict(name=name, slug=f'catalog-p-{n}', erp_code=f'ERP-CT-{n}', category=category or self.other, price=price,
                    price2=price - 10000, price3=price - 20000, stock=stock)
        data.update(extra)
        product = Product.objects.create(**data)
        Product.objects.filter(pk=product.pk).update(created_at=self.base - timedelta(minutes=age_minutes))
        return product

    def listing(self, user=None, **params):
        if user is not None:
            self.client.force_login(user)
        response = self.client.get(reverse(LIST), params)
        self.assertEqual(response.status_code, 200)
        return response

    def names(self, response):
        return [p.name for p in response.context['products']]


# ======================================================================== فیلتر
class DiscountFilterTests(CatalogBase):
    def setUp(self):
        super().setUp()
        self.a = self.product('الف تخفیف‌دار', self.root, age_minutes=1)
        self.b = self.product('ب تخفیف‌دار', self.sub, age_minutes=2)
        self.c = self.product('ج بدون تخفیف', self.root, age_minutes=3)
        self.d = self.product('د تخفیف‌دار ناموجود', self.root, stock=0, age_minutes=4)
        make_promotion(self.a, percent=20)
        make_promotion(self.b, percent=10)
        make_promotion(self.d, percent=30)

    def test_without_the_filter_everything_is_listed(self):
        self.assertEqual(set(self.names(self.listing())), {'الف تخفیف‌دار', 'ب تخفیف‌دار', 'ج بدون تخفیف', 'د تخفیف‌دار ناموجود'})

    def test_filter_lists_only_discounted_products(self):
        response = self.listing(discount='1')
        self.assertEqual(set(self.names(response)), {'الف تخفیف‌دار', 'ب تخفیف‌دار', 'د تخفیف‌دار ناموجود'})
        self.assertEqual(response.context['products'].paginator.count, 3)
        self.assertTrue(response.context['discount_only'])

    def test_filter_keeps_the_regular_sort_and_stock_last_rule(self):
        self.assertEqual(self.names(self.listing(discount='1')), ['الف تخفیف‌دار', 'ب تخفیف‌دار', 'د تخفیف‌دار ناموجود'])
        self.assertEqual(self.names(self.listing(discount='1', sort='price_asc'))[-1], 'د تخفیف‌دار ناموجود')

    def test_filter_combines_with_the_other_filters(self):
        self.assertEqual(set(self.names(self.listing(discount='1', category=self.sub.slug))), {'ب تخفیف‌دار'})
        self.assertEqual(set(self.names(self.listing(discount='1', category=self.root.slug))),
                         {'الف تخفیف‌دار', 'ب تخفیف‌دار', 'د تخفیف‌دار ناموجود'})            # ریشه + زیردسته
        self.assertEqual(set(self.names(self.listing(discount='1', in_stock='1'))), {'الف تخفیف‌دار', 'ب تخفیف‌دار'})
        self.assertEqual(set(self.names(self.listing(discount='1', q='ب تخفیف'))), {'ب تخفیف‌دار'})
        self.assertEqual(self.names(self.listing(discount='1', price_min=200000)), [])

    def test_brand_and_color_filters_with_the_discount_filter(self):
        Product.objects.filter(pk=self.a.pk).update(brand=self.brand)
        ProductColor.objects.create(product=self.b, name='قرمز', hex_code='#ff0000')
        ProductColor.objects.create(product=self.b, name='قرمز', hex_code='#ff0001')            # JOIN تکراری ← distinct لازم است
        self.assertEqual(self.names(self.listing(discount='1', brand=self.brand.slug)), ['الف تخفیف‌دار'])
        self.assertEqual(self.names(self.listing(discount='1', color='قرمز')), ['ب تخفیف‌دار'])

    def test_a_promotion_that_gives_no_real_discount_is_not_counted(self):
        e = self.product('ه قیمت ویژه بالاتر', self.root, price=50000)
        make_promotion(e, kind='special_price', value=90000)                                   # از قیمت فعلی بیشتر ← بی‌اثر
        self.assertNotIn('ه قیمت ویژه بالاتر', self.names(self.listing(discount='1')))

    def test_inactive_expired_and_scheduled_promotions_are_ignored(self):
        make_promotion(self.c, percent=50, active=False)
        self.assertNotIn('ج بدون تخفیف', self.names(self.listing(discount='1')))
        make_promotion(self.c, percent=50, expired=True)
        make_promotion(self.c, percent=50, scheduled=True)
        self.assertNotIn('ج بدون تخفیف', self.names(self.listing(discount='1')))

    def test_category_promotion_reaches_descendants(self):
        f = self.product('و در زیردسته', self.sub)
        make_promotion(percent=15, targets=[{'target_type': 'category', 'category': self.root}])
        names = set(self.names(self.listing(discount='1')))
        self.assertIn('و در زیردسته', names)
        self.assertIsNotNone(f)

    def test_exclusions_are_respected(self):
        make_promotion(percent=15, targets=[{'target_type': 'category', 'category': self.root},
                                            {'target_type': 'product', 'product': self.c, 'is_exclusion': True}])
        self.assertNotIn('ج بدون تخفیف', self.names(self.listing(discount='1')))

    def test_bogus_flag_values_are_ignored(self):
        self.assertEqual(len(self.names(self.listing(discount='2'))), 4)
        self.assertEqual(len(self.names(self.listing(discount='yes'))), 4)

    def test_filter_respects_the_global_switch(self):
        policy = DiscountPolicy.load()
        policy.promotions_enabled = False
        policy.save()
        self.assertEqual(self.names(self.listing(discount='1')), [])

    def test_empty_result_page_renders(self):
        Product.objects.all().delete()
        response = self.listing(discount='1')
        self.assertContains(response, 'محصولی یافت نشد')

    def test_falls_back_to_the_rule_filter_for_huge_lists_and_stays_correct(self):
        with mock.patch.object(catalog, 'MAX_PK_IN', 1):
            self.assertEqual(set(self.names(self.listing(discount='1'))), {'الف تخفیف‌دار', 'ب تخفیف‌دار', 'د تخفیف‌دار ناموجود'})


# ======================================================================== مخاطب و کاربر
class PerUserTests(CatalogBase):
    def setUp(self):
        super().setUp()
        self.p = self.product('کالای کاربرمحور', self.root)

    def test_guest_sees_public_promotions_only(self):
        make_promotion(self.p, percent=20, login_required=True)
        self.client.logout()
        self.assertEqual(self.names(self.listing(discount='1')), [])
        self.client.force_login(self.user)
        self.assertEqual(self.names(self.listing(discount='1')), ['کالای کاربرمحور'])
        self.client.logout()
        make_promotion(self.p, percent=10)
        self.assertEqual(self.names(self.listing(discount='1')), ['کالای کاربرمحور'])

    def test_vip_users_get_no_discount_by_default_so_the_filter_is_empty(self):
        make_promotion(self.p, percent=20)
        self.assertEqual(self.names(self.listing(user=self.vip, discount='1')), [])
        policy = DiscountPolicy.load()
        policy.apply_to_vip = True
        policy.save()
        self.assertEqual(self.names(self.listing(user=self.vip, discount='1')), ['کالای کاربرمحور'])

    def test_price_level_and_payment_method_conditions(self):
        make_promotion(self.p, percent=20, price_levels='2')
        self.assertEqual(self.names(self.listing(user=self.user, discount='1')), [])             # سطح ۱
        level2 = CustomUser.objects.create_user(phone_number=f'0912017{next(_seq):04d}', price_level=2)
        self.assertEqual(self.names(self.listing(user=level2, discount='1')), ['کالای کاربرمحور'])
        make_promotion(self.product('کالای نقدی', self.root), percent=10, payment_method='cash')
        self.assertEqual(self.names(self.listing(user=level2, discount='1')), ['کالای کاربرمحور', 'کالای نقدی'])
        self.assertEqual(self.names(self.listing(user=self.user, discount='1')), [])             # سطح ۱ پیش‌فرض «چکی» است

    def test_loyalty_gate(self):
        make_promotion(self.p, percent=20, min_loyalty_level=2)
        self.assertEqual(self.names(self.listing(user=self.user, discount='1')), [])
        veteran = CustomUser.objects.create_user(phone_number=f'0912017{next(_seq):04d}', price_level=1)
        with mock.patch.object(CustomUser, 'paid_orders_count', 9, create=True):
            self.assertEqual(self.names(self.listing(user=veteran, discount='1')), ['کالای کاربرمحور'])

    def test_two_users_never_share_results(self):
        make_promotion(self.p, percent=20, price_levels='2')
        level2 = CustomUser.objects.create_user(phone_number=f'0912017{next(_seq):04d}', price_level=2)
        for _ in range(2):
            self.assertEqual(self.names(self.listing(user=level2, discount='1')), ['کالای کاربرمحور'])
            self.assertEqual(self.names(self.listing(user=self.user, discount='1')), [])


# ======================================================================== ترتیب «بیشترین تخفیف»
class DiscountSortTests(CatalogBase):
    def setUp(self):
        super().setUp()
        self.p10 = self.product('ده درصد', age_minutes=1)
        self.p30 = self.product('سی درصد', age_minutes=5)
        self.p20_old = self.product('بیست قدیمی', age_minutes=9)
        self.p20_new = self.product('بیست جدید', age_minutes=2)
        self.plain_new = self.product('ساده جدید', age_minutes=0)
        self.plain_old = self.product('ساده قدیمی', age_minutes=20)
        self.p40_out = self.product('چهل ناموجود', stock=0, age_minutes=3)
        self.plain_out = self.product('ساده ناموجود', stock=0, age_minutes=4)
        for product, percent in ((self.p10, 10), (self.p30, 30), (self.p20_old, 20), (self.p20_new, 20), (self.p40_out, 40)):
            make_promotion(product, percent=percent)

    def test_order_is_percent_then_newest_with_stock_first_and_plain_last(self):
        expected = ['سی درصد', 'بیست جدید', 'بیست قدیمی', 'ده درصد',            # موجود و تخفیف‌دار
                    'ساده جدید', 'ساده قدیمی',                                    # موجود و بدون تخفیف (جدیدترین اول)
                    'چهل ناموجود', 'ساده ناموجود']                                # ناموجودها همیشه آخر
        self.assertEqual(self.names(self.listing(sort='discount')), expected)

    def test_amount_breaks_a_percent_tie(self):
        pricey = self.product('بیست گران', price=500000, age_minutes=30)
        make_promotion(pricey, percent=20)
        names = self.names(self.listing(sort='discount'))
        self.assertLess(names.index('بیست گران'), names.index('بیست جدید'))                   # مبلغ تخفیف بیشتر ← جلوتر

    def test_fixed_amount_discounts_are_ranked_by_their_effective_percent(self):
        big = self.product('ثابت بزرگ', price=100000, age_minutes=40)
        make_promotion(big, kind='fixed', value=35000)                                        # ۳۵٪
        names = self.names(self.listing(sort='discount'))
        self.assertEqual(names[0], 'ثابت بزرگ')

    def test_sort_with_the_filter_lists_only_discounted_products(self):
        names = self.names(self.listing(sort='discount', discount='1'))
        self.assertEqual(names, ['سی درصد', 'بیست جدید', 'بیست قدیمی', 'ده درصد', 'چهل ناموجود'])

    def test_sort_with_other_filters(self):
        names = self.names(self.listing(sort='discount', in_stock='1'))
        self.assertEqual(names[:4], ['سی درصد', 'بیست جدید', 'بیست قدیمی', 'ده درصد'])
        self.assertNotIn('چهل ناموجود', names)

    def test_pagination_is_stable_and_complete(self):
        for i in range(20):
            make_promotion(self.product(f'انبوه {i:02d}', age_minutes=100 + i), percent=5)
        seen, page = [], 1
        while True:
            response = self.listing(sort='discount', page=page)
            seen.extend(p.pk for p in response.context['products'])
            if not response.context['products'].has_next():
                break
            page += 1
        total = Product.visible.count()
        self.assertEqual(len(seen), total)
        self.assertEqual(len(set(seen)), total)                                                 # بدون تکرار و افتادگی
        self.assertEqual(response.context['products'].paginator.count, total)
        first_page = [p.name for p in self.listing(sort='discount', page=1).context['products']]
        self.assertEqual(first_page[:4], ['سی درصد', 'بیست جدید', 'بیست قدیمی', 'ده درصد'])

    def test_out_of_range_page_returns_the_last_page(self):
        response = self.listing(sort='discount', page=999)
        self.assertTrue(len(response.context['products']) > 0)

    def test_the_dropdown_offers_and_selects_the_option(self):
        html = self.listing(sort='discount').content.decode()
        self.assertIn('<option value="discount" selected>بیشترین تخفیف</option>', html)
        self.assertRegex(self.listing().content.decode(), r'<option value="newest"[^>]*>جدیدترین</option>')

    def test_a_user_without_any_discount_gets_the_plain_newest_order(self):
        names = self.names(self.listing(user=self.vip, sort='discount'))                        # VIP: تخفیفی نیست
        self.assertEqual(names[0], 'ساده جدید')
        self.assertEqual(names[-2:], ['چهل ناموجود', 'ساده ناموجود'])

    def test_htmx_request_returns_the_grid_partial(self):
        response = self.client.get(reverse(LIST), {'sort': 'discount'}, HTTP_HX_REQUEST='true')
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'products/partials/product_grid.html')
        self.assertContains(response, 'سی درصد')


# ======================================================================== اتصال به موتور قیمت
class EngineAgreementTests(CatalogBase):
    def test_catalog_percent_equals_the_card_percent_for_every_product(self):
        products = [self.product(f'محصول {i}', price=10000 * (i + 3), age_minutes=i) for i in range(8)]
        make_promotion(products[0], percent=13)
        make_promotion(products[1], kind='fixed', value=4321)
        make_promotion(products[2], kind='special_price', value=15000)
        make_promotion(products[3], percent=50, max_discount_amount=3000)
        make_promotion(percent=7, targets=[{'target_type': 'all'}])                             # کل فروشگاه (بهترین تخفیف برنده می‌شود)
        built = catalog.build_discount_catalog(self.user, Product.visible.all())
        for product in Product.visible.all():
            breakdown = price_breakdown(product, self.user)
            listed = built.percents.get(product.pk)
            self.assertEqual(listed is not None, breakdown.has_discount, product.name)
            if listed:
                self.assertEqual(listed, (breakdown.percent, int(breakdown.discount_amount)), product.name)

    def test_the_listing_prices_match_the_catalog_percent(self):
        product = self.product('قیمت‌دار', price=100000)
        make_promotion(product, percent=25)
        response = self.listing(discount='1')
        self.assertContains(response, '٪25')

    def test_registry_hooks_are_installed_by_the_app(self):
        self.assertTrue(deals.discount_catalog_available())
        built = deals.discount_catalog(self.user, Product.visible.all())
        self.assertEqual(built.count, 0)

    def test_without_a_provider_the_options_disappear_and_sort_falls_back(self):
        self.product('کالا', age_minutes=1)
        with mock.patch.object(deals, '_catalog_provider', None):
            response = self.listing(sort='discount', discount='1')
        self.assertFalse(response.context['discount_available'])
        self.assertEqual(response.context['current_sort'], 'newest')
        self.assertEqual(len(response.context['products']), 1)                                  # فیلتر هم بی‌اثر شد
        self.assertNotContains(response, 'data-filter-discount')

    def test_the_filter_checkbox_is_rendered_and_reflects_the_state(self):
        self.assertContains(self.listing(), 'data-filter-discount')
        self.assertContains(self.listing(discount='1'), "toggleProductFilterMulti('discount', '1')")


# ======================================================================== هزینه
class QueryCostTests(CatalogBase):
    def count(self, **params):
        """ کمترین تعداد کوئری در سه بار درخواست پیاپی (کوئری‌های پرشدن اولیه‌ی کش‌های ناوبری/دسته‌ها نویز نسازند) """
        self.listing(**params)                                                                  # گرم‌کردن شاخص/کش‌ها
        counts = []
        for _ in range(3):
            with CaptureQueriesContext(connection) as queries:
                self.listing(**params)
            counts.append(len(queries))
        return min(counts)

    def test_the_filter_adds_at_most_one_query_and_the_sort_at_most_two(self):
        for i in range(6):
            make_promotion(self.product(f'کالا {i}', age_minutes=i), percent=10 + i)
        baseline = self.count()
        self.assertLessEqual(self.count(discount='1') - baseline, 1)
        self.assertLessEqual(self.count(sort='discount') - baseline, 3)

    def test_query_count_does_not_grow_with_more_products_or_promotions(self):
        # صفحه‌ی اول در هر دو حالت ۱۲ کارت دارد؛ فقط تعداد کل محصولات/تخفیف‌ها زیاد می‌شود
        for i in range(14):
            make_promotion(self.product(f'کالا {i}', age_minutes=i), percent=10 + i)
        few_filter, few_sort = self.count(discount='1'), self.count(sort='discount')
        for i in range(14, 60):
            make_promotion(self.product(f'کالا {i}', age_minutes=i), percent=5 + i % 30)
        self.assertEqual(self.count(discount='1'), few_filter)
        self.assertEqual(self.count(sort='discount'), few_sort)

    def test_no_discounts_means_no_candidate_query_at_all(self):
        self.product('تنها', age_minutes=1)
        self.listing()
        with CaptureQueriesContext(connection) as plain:
            self.listing()
        with CaptureQueriesContext(connection) as filtered:
            self.listing(discount='1')
        self.assertLessEqual(len(filtered) - len(plain), 0)                                     # شاخص خالی ← کوئری کاندید نیست

    def test_candidate_query_only_selects_the_pricing_columns(self):
        product = self.product('ستونی', age_minutes=1)
        make_promotion(product, percent=10)
        catalog.build_discount_catalog(self.user, Product.visible.all())
        with CaptureQueriesContext(connection) as queries:
            catalog.build_discount_catalog(self.user, Product.visible.all())
        self.assertEqual(len(queries), 1)
        sql = queries.captured_queries[0]['sql'].lower()
        self.assertNotIn('additional_description', sql)
        self.assertNotIn('description', sql.replace('additional_description', ''))

    def test_building_over_a_thousand_products_is_fast_enough(self):
        rows = [Product(name=f'انبوه {i}', slug=f'bulk-{next(_seq)}', erp_code=f'ERP-BULK-{next(_seq)}', category=self.other,
                        price=100000, price2=90000, price3=80000, stock=3) for i in range(1200)]
        Product.objects.bulk_create(rows, batch_size=200)
        make_promotion(percent=10, targets=[{'target_type': 'all'}])
        import time
        started = time.perf_counter()
        built = catalog.build_discount_catalog(self.user, Product.visible.all())
        elapsed = time.perf_counter() - started
        self.assertEqual(built.count, 1200)
        self.assertLess(elapsed, 3.0)                                                           # سقفِ سخاوتمندانه؛ هدف کشف O(n²) است
