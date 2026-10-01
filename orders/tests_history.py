"""
تست‌های «سفارش‌های من» (orders/history.py + templates/orders/history.html): تب‌ها و شمارنده‌ها، جستجوی ترکیبی،
فیلترها، صفحه‌بندی، تعداد کوئری ثابت (بدون N+1)، کارت سفارش/مرجوعی و حالت خالی.
"""
from datetime import timedelta
from itertools import count

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from orders.history import MAX_THUMBS, PAGE_SIZE, parse_search
from orders.models import Order
from payments.models import Transaction
from products.models import Product
from returns.models import ReturnItem, ReturnRequest
from returns.tests import ReturnsTestMixin

_seq = count(1)


class HistoryBase(ReturnsTestMixin, TestCase):
    def setUp(self):
        self.user = self.make_user()
        self.category = self.make_category()
        self.client.force_login(self.user)
        self.url = reverse('orders:order_history')
        self.reason = self.make_reason()

    def product(self, name='کالای آزمون'):
        n = next(_seq)
        return Product.objects.create(name=name, slug=f'hist-p{n}', erp_code=f'ERP-H-{n}', category=self.category,
                                      price=100000, stock=10)

    def order(self, status='shipped', paid=True, products=None, user=None, total=200000, **extra):
        user = user or self.user
        order = self.make_order(user, status=status, total_price=total, **extra)
        for product in (products or [self.product()]):
            self.make_order_item(order, product, quantity=1)
        if paid:
            Transaction.objects.create(user=user, order=order, amount=total, authority=f'AUTH-H-{next(_seq)}',
                                       status='success')
        return order

    def ids(self, response, key='orders'):
        return [o.pk for o in response.context[key]]

    def counts(self, response):
        return {t['key']: t['count'] for t in response.context['tabs']}

    def get(self, **params):
        return self.client.get(self.url, params)


class TabTests(HistoryBase):
    def test_default_tab_is_current_and_excludes_delivered_and_canceled(self):
        shipped = self.order('shipped')
        processing = self.order('processing')
        unpaid_pending = self.order('pending', paid=False)
        self.order('delivered')
        self.order('canceled', paid=False)
        response = self.get()
        self.assertEqual(response.context['tab'], 'current')
        self.assertCountEqual(self.ids(response), [shipped.pk, processing.pk, unpaid_pending.pk])

    def test_delivered_tab_only_lists_paid_delivered_orders(self):
        delivered = self.order('delivered')
        unpaid_delivered = self.order('delivered', paid=False)      # از دید مشتری هنوز «در انتظار پرداخت» است
        self.order('shipped')
        response = self.get(tab='delivered')
        self.assertEqual(self.ids(response), [delivered.pk])
        self.assertIn(unpaid_delivered.pk, self.ids(self.get()))      # این یکی در تب جاری است

    def test_canceled_tab(self):
        canceled = self.order('canceled', paid=False)
        self.order('shipped')
        self.assertEqual(self.ids(self.get(tab='canceled')), [canceled.pk])

    def test_returned_tab_lists_return_requests_with_items_and_reason(self):
        product = self.product('کابل شارژ سریع')
        order = self.order('delivered', products=[product])
        item = order.items.get()
        request_obj = self.make_return_request(order, self.user)
        ReturnItem.objects.create(return_request=request_obj, order_item=item, reason=self.reason, requested_quantity=1)
        response = self.get(tab='returned')
        self.assertEqual(self.ids(response, 'return_requests'), [request_obj.pk])
        self.assertContains(response, 'کابل شارژ سریع')
        self.assertContains(response, 'مغایرت با تصویر')
        self.assertContains(response, request_obj.get_status_display())
        self.assertContains(response, f'#{request_obj.pk}')
        self.assertEqual(response.context['orders'], [])

    def test_counters_per_tab(self):
        self.order('shipped')
        self.order('pending', paid=False)
        self.order('delivered')
        delivered_returned = self.order('delivered')
        self.order('canceled', paid=False)
        self.make_return_request(delivered_returned, self.user)
        self.assertEqual(self.counts(self.get()), {'current': 2, 'delivered': 2, 'returned': 1, 'canceled': 1})

    def test_other_users_data_is_never_listed_or_counted(self):
        other = self.make_user('09141112222')
        foreign = self.order('shipped', user=other)
        self.make_return_request(self.order('delivered', user=other), other)
        mine = self.order('shipped')
        response = self.get()
        self.assertEqual(self.ids(response), [mine.pk])
        self.assertNotIn(foreign.pk, self.ids(response))
        self.assertEqual(self.counts(response), {'current': 1, 'delivered': 0, 'returned': 0, 'canceled': 0})

    def test_legacy_status_param_maps_to_a_tab(self):
        delivered = self.order('delivered')
        self.assertEqual(self.get(status='delivered').context['tab'], 'delivered')
        self.assertEqual(self.ids(self.get(status='delivered')), [delivered.pk])
        self.assertEqual(self.get(status='canceled').context['tab'], 'canceled')
        self.assertEqual(self.get(status='shipped').context['tab'], 'current')
        self.assertEqual(self.get(tab='nonsense').context['tab'], 'current')

    def test_active_tab_markup_and_badges(self):
        self.order('shipped')
        html = self.get(tab='delivered').content.decode()
        self.assertEqual(html.count('aria-current="page"'), 1)
        self.assertIn('تحویل شده', html)
        self.assertIn('مرجوع شده', html)
        self.assertIn('لغو شده', html)
        self.assertIn('class="oh-badge">1</span>', html)


class SearchTests(HistoryBase):
    def test_parse_search_handles_persian_digits_hash_and_names(self):
        self.assertEqual(parse_search('۱۲۳')[0], 123)
        self.assertEqual(parse_search('#45')[0], 45)
        self.assertEqual(parse_search('')[0], None)
        order_id, name = parse_search('كابل')                                 # ك عربی
        self.assertIsNone(order_id)
        self.assertEqual(name, parse_search('کابل')[1])                        # با ک فارسی یکی می‌شود
        self.assertIsNone(parse_search('9' * 30)[0])                           # عدد خیلی بزرگ: شماره سفارش حساب نمی‌شود

    def test_search_by_order_id_latin_and_persian_digits(self):
        target = self.order('shipped')
        self.order('shipped')
        persian = str(target.pk).translate(str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹'))
        for query in (str(target.pk), persian, f'#{target.pk}'):
            with self.subTest(query=query):
                self.assertEqual(self.ids(self.get(q=query)), [target.pk])

    def test_search_by_product_name_is_normalized(self):
        cable = self.order('shipped', products=[self.product('کابل شارژ ۱.۸ متر')])
        self.order('shipped', products=[self.product('هدفون بی‌سیم')])
        for query in ('کابل', 'كابل', 'شارژ'):
            with self.subTest(query=query):
                self.assertEqual(self.ids(self.get(q=query)), [cable.pk])

    def test_order_with_two_matching_items_is_not_duplicated(self):
        order = self.order('shipped', products=[self.product('کابل نوع یک'), self.product('کابل نوع دو')])
        response = self.get(q='کابل')
        self.assertEqual(self.ids(response), [order.pk])
        self.assertEqual(self.counts(response)['current'], 1)

    def test_search_applies_to_counters_and_all_tabs(self):
        self.order('shipped', products=[self.product('لپ‌تاپ')])
        self.order('delivered', products=[self.product('لپ‌تاپ دوم')])
        self.order('delivered', products=[self.product('موس')])
        response = self.get(q='لپ‌تاپ')
        self.assertEqual(self.counts(response), {'current': 1, 'delivered': 1, 'returned': 0, 'canceled': 0})

    def test_search_in_returned_tab_matches_order_id_and_product_name(self):
        product = self.product('شارژر دیواری')
        order = self.order('delivered', products=[product])
        request_obj = self.make_return_request(order, self.user)
        ReturnItem.objects.create(return_request=request_obj, order_item=order.items.get(), reason=self.reason, requested_quantity=1)
        other = self.order('delivered')
        self.make_return_request(other, self.user)
        self.assertEqual(self.ids(self.get(tab='returned', q='شارژر'), 'return_requests'), [request_obj.pk])
        self.assertEqual(self.ids(self.get(tab='returned', q=str(other.pk)), 'return_requests'),
                         [ReturnRequest.objects.get(order=other).pk])

    def test_empty_search_result_hints_at_other_tabs(self):
        self.order('delivered', products=[self.product('لپ‌تاپ')])
        response = self.get(q='لپ‌تاپ')                                         # تب جاری خالی، تحویل‌شده یک مورد
        self.assertEqual(self.ids(response), [])
        self.assertEqual(response.context['other_matches'], 1)
        self.assertContains(response, 'در تب‌های دیگر 1 مورد')
        self.assertContains(response, 'پاک کردن جستجو و فیلترها')

    def test_search_input_keeps_the_query_and_tab_links_preserve_it(self):
        self.order('shipped', products=[self.product('لپ‌تاپ')])
        response = self.get(q='لپ‌تاپ')
        self.assertContains(response, 'value="لپ‌تاپ"')
        urls = {t['key']: t['url'] for t in response.context['tabs']}
        self.assertIn('q=', urls['delivered'])
        self.assertIn('tab=delivered', urls['delivered'])
        self.assertNotIn('tab=', urls['current'])


class FilterAndPaginationTests(HistoryBase):
    def test_date_and_amount_filters_still_work(self):
        recent = self.order('shipped', total=300000)
        old = self.order('shipped', total=3000000)
        Order.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=60))
        self.assertEqual(self.ids(self.get(date_range='30days')), [recent.pk])
        self.assertEqual(self.ids(self.get(amount_range='less500')), [recent.pk])
        self.assertEqual(self.ids(self.get(amount_range='1000-5000')), [old.pk])
        self.assertTrue(self.get(date_range='30days').context['filters_active'])
        self.assertEqual(self.counts(self.get(amount_range='less500'))['current'], 1)

    def test_pagination_splits_pages_and_keeps_query(self):
        for _ in range(PAGE_SIZE + 2):
            self.order('shipped', products=[self.product('کالای تکراری')])
        first = self.get(q='تکراری')
        self.assertEqual(len(first.context['orders']), PAGE_SIZE)
        self.assertContains(first, 'q=%D8%AA%DA%A9%D8%B1%D8%A7%D8%B1%DB%8C&page=2')
        second = self.get(q='تکراری', page=2)
        self.assertEqual(len(second.context['orders']), 2)
        self.assertContains(second, 'صفحه 2 از 2')
        self.assertEqual(len(self.get(page='abc').context['orders']), PAGE_SIZE)      # صفحه‌ی نامعتبر: صفحه‌ی اول
        self.assertEqual(len(self.get(page=99).context['orders']), 2)                 # خارج از بازه: آخرین صفحه

    def test_orders_are_newest_first(self):
        first = self.order('shipped')
        second = self.order('shipped')
        self.assertEqual(self.ids(self.get()), [second.pk, first.pk])


class QueryCountTests(HistoryBase):
    def _queries(self):
        self.get()                                                                 # گرم‌کردن (کش تنظیمات سایت، سشن و ...)
        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(self.get().status_code, 200)
        return len(ctx)

    def test_query_count_does_not_grow_with_number_of_orders(self):
        self.order('shipped', products=[self.product(), self.product()])
        baseline = self._queries()
        for _ in range(8):
            self.order('shipped', products=[self.product(), self.product(), self.product()])
        self.assertEqual(self._queries(), baseline)

    def test_returned_tab_query_count_is_flat_too(self):
        def make():
            order = self.order('delivered')
            request_obj = self.make_return_request(order, self.user)
            ReturnItem.objects.create(return_request=request_obj, order_item=order.items.get(), reason=self.reason, requested_quantity=1)
        make()
        self.get(tab='returned')                                                   # گرم‌کردن
        with CaptureQueriesContext(connection) as ctx:
            self.get(tab='returned')
        baseline = len(ctx)
        for _ in range(6):
            make()
        with CaptureQueriesContext(connection) as ctx:
            self.get(tab='returned')
        self.assertEqual(len(ctx), baseline)

    def test_annotated_paid_flag_avoids_per_order_payment_queries(self):
        from orders.history import base_orders
        self.order('delivered')
        orders = list(base_orders(self.user, {}))
        with self.assertNumQueries(0):
            self.assertTrue(orders[0].is_paid)
            self.assertEqual(orders[0].customer_status, 'delivered')
            self.assertTrue(orders[0].can_review)
        plain = Order.objects.get(pk=orders[0].pk)
        with self.assertNumQueries(1):                                             # بدون annotate: رفتار قبلی (یک exists)
            self.assertTrue(plain.is_paid)


class CardRenderingTests(HistoryBase):
    def test_card_shows_status_date_code_amount_and_thumbs(self):
        products = [self.product(f'کالا {i}') for i in range(3)]
        order = self.order('shipped', products=products, total=450000)
        html = self.get().content.decode()
        self.assertIn('ارسال شده', html)
        self.assertIn(f'#{order.pk}', html)
        self.assertIn('450000 تومان', html)
        self.assertEqual(html.count('class="oh-thumb"'), 3)
        self.assertIn(reverse('orders:order_detail_full', args=[order.pk]), html)
        self.assertNotIn('oh-more', html)

    def test_more_items_chip_and_quantity_badge(self):
        products = [self.product(f'کالا {i}') for i in range(MAX_THUMBS + 3)]
        order = self.order('shipped', products=products)
        item = order.items.first()
        item.quantity = 3
        item.save()
        html = self.get().content.decode()
        self.assertEqual(html.count('class="oh-thumb"'), MAX_THUMBS)
        self.assertIn('+3', html)
        self.assertIn('<span class="oh-qty">3</span>', html)

    def test_review_link_only_on_delivered_cards_and_pay_link_on_unpaid(self):
        delivered = self.order('delivered')
        self.assertContains(self.get(tab='delivered'), f'href="{reverse("orders:order_reviews", args=[delivered.pk])}"')
        unpaid = self.order('pending', paid=False)
        current = self.get()
        self.assertNotContains(current, reverse('orders:order_reviews', args=[unpaid.pk]))
        self.assertContains(current, reverse('payments:start_payment', args=[unpaid.pk]))

    def test_empty_states_per_tab(self):
        expectations = {
            'current': 'سفارش جاری ندارید', 'delivered': 'هنوز سفارش تحویل‌شده‌ای ندارید',
            'returned': 'هنوز درخواست مرجوعی ثبت نکرده‌اید', 'canceled': 'سفارش لغوشده‌ای ندارید',
        }
        for tab, text in expectations.items():
            with self.subTest(tab=tab):
                response = self.get(tab=tab)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, text)

    def test_anonymous_user_is_redirected_to_login(self):
        self.client.logout()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response.url)


class MobileTabsCssTests(HistoryBase):
    def test_tabs_fit_the_screen_on_mobile_without_horizontal_scroll(self):
        """ زیر ۶۴۰px هر ۴ تب ستون مساوی‌اند (grid) و اسکرول افقی ندارند؛ فونت تب text-xs و بج ۱۰px """
        from pathlib import Path
        from django.conf import settings
        css = (Path(settings.BASE_DIR) / 'static/theme/assets/css/app.css').read_text(encoding='utf-8')
        mobile = css[css.index('@media (max-width: 639px) {\r\n  .oh-tabs') if '\r\n' in css else css.index('@media (max-width: 639px) {\n  .oh-tabs'):]
        self.assertIn('grid-template-columns: repeat(4, minmax(0, 1fr))', mobile)
        self.assertIn('overflow: visible', mobile)
        self.assertIn('font-size: .75rem', mobile)
        self.assertIn('font-size: 10px', mobile)

    def test_all_four_tabs_are_always_rendered(self):
        html = self.get().content.decode()
        self.assertEqual(html.count('class="oh-tab '), 4)
