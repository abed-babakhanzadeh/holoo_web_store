"""
تست پخش متناسب تخفیف سطح سفارش روی فی اقلام فاکتور هلو (holoo/invoice.py::allocate_discount / item_lines).

هدف: جمع ردیف‌های فاکتور هلو (+ ردیف کرایه‌ی پیک) دقیقاً برابر «مبلغ قابل‌پرداخت» مشتری (Order.total_price) باشد،
بدون حتی یک ریال اختلاف، و بدون اینکه فیِ هیچ ردیفی از قیمت اصلی‌اش بیشتر یا منفی شود.
"""

import random
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase

from accounts.models import CustomUser
from holoo.invoice import allocate_discount, build_invoice_payload, invoice_comment, item_lines, payload_total
from holoo.tasks import send_order_to_holoo
from orders.models import Order, OrderItem
from products.models import Category, Product


def totals(parts):
    return sum(price * quantity for pieces in parts for price, quantity in pieces)


class AllocateDiscountTests(SimpleTestCase):
    def test_no_discount_leaves_rows_untouched(self):
        rows = [(100000, 2), (50000, 1)]
        self.assertEqual(allocate_discount(rows, 0), [[(100000, 2)], [(50000, 1)]])

    def test_empty_rows(self):
        self.assertEqual(allocate_discount([], 5000), [])

    def test_single_row_gets_the_whole_discount(self):
        self.assertEqual(allocate_discount([(100000, 1)], 12345), [[(87655, 1)]])

    def test_exact_proportional_split(self):
        # ۱۰۰٬۰۰۰ و ۳۰۰٬۰۰۰ ← تخفیف ۴۰٬۰۰۰: سهم‌ها ۱۰٬۰۰۰ و ۳۰٬۰۰۰
        self.assertEqual(allocate_discount([(100000, 1), (300000, 1)], 40000), [[(90000, 1)], [(270000, 1)]])

    def test_the_share_is_proportional_to_the_row_amount_not_the_unit_price(self):
        # ردیف اول ۱۰×۱۰٬۰۰۰ = ۱۰۰٬۰۰۰؛ ردیف دوم ۱×۱۰۰٬۰۰۰ = ۱۰۰٬۰۰۰ ← هر کدام نصف تخفیف
        parts = allocate_discount([(10000, 10), (100000, 1)], 20000)
        self.assertEqual(parts, [[(9000, 10)], [(90000, 1)]])

    def test_penny_rounding_goes_to_the_last_row(self):
        parts = allocate_discount([(1000, 1), (1000, 1), (1000, 1)], 100)
        self.assertEqual(totals(parts), 2900)
        self.assertEqual([pieces[0][0] for pieces in parts], [966, 966, 968])            # ۲ ریال باقی‌مانده روی ردیف آخر

    def test_remainder_not_divisible_by_the_last_quantity_splits_only_that_row(self):
        # جمع ۵٬۰۰۰ و تخفیف ۱۰۱ ← هدف ۴٬۸۹۹؛ فیِ متناسب ۹۷۹ (کف ۹۷۹٫۸) و جمعِ حاصل ۴٬۸۹۵ است، یعنی ۴ ریال باقی‌مانده.
        # روی ردیف آخر (۳ واحد) ۴ = ۱ ریال برای هر واحد + ۱ ریالِ اضافه ← دو بخش که فی‌شان یک ریال فرق دارد
        parts = allocate_discount([(1000, 1), (1000, 1), (1000, 3)], 101)
        self.assertEqual(totals(parts), 5000 - 101)
        self.assertEqual([len(pieces) for pieces in parts], [1, 1, 2])
        self.assertEqual(parts[2], [(981, 1), (980, 2)])
        (high_price, high_qty), (low_price, low_qty) = parts[2]
        self.assertEqual(high_price - low_price, 1)
        self.assertEqual(high_qty + low_qty, 3)

    def test_rows_that_divide_evenly_are_never_split(self):
        # ۱۰٪ از ۱۶٬۰۰۰ = ۱٬۶۰۰؛ فی‌ها دقیقاً ۱٬۸۰۰ و ۲٬۷۰۰ می‌شوند و باقی‌مانده‌ای نمی‌ماند
        parts = allocate_discount([(2000, 5), (3000, 2)], 1600)
        self.assertEqual(parts, [[(1800, 5)], [(2700, 2)]])
        self.assertEqual(totals(parts), 16000 - 1600)

    def test_discount_larger_than_the_total_is_capped(self):
        parts = allocate_discount([(1000, 2), (500, 1)], 10 ** 9)
        self.assertEqual(totals(parts), 0)
        self.assertTrue(all(price == 0 for pieces in parts for price, _ in pieces))

    def test_full_discount_equal_to_the_total(self):
        self.assertEqual(totals(allocate_discount([(1000, 2), (500, 1)], 2500)), 0)

    def test_negative_discount_is_treated_as_zero(self):
        self.assertEqual(allocate_discount([(1000, 1)], -50), [[(1000, 1)]])

    def test_decimal_and_string_like_inputs_are_accepted(self):
        parts = allocate_discount([(Decimal('100000'), 2)], Decimal('20000'))
        self.assertEqual(parts, [[(90000, 2)]])

    def test_a_zero_priced_row_stays_zero(self):
        parts = allocate_discount([(0, 3), (1000, 1)], 100)
        self.assertEqual(parts[0], [(0, 3)])
        self.assertEqual(totals(parts), 900)

    def test_when_the_last_row_cannot_absorb_the_remainder_earlier_rows_do(self):
        # آخرین ردیف ارزان و تک‌تایی؛ باقی‌مانده‌ی گرد کردنِ ردیف‌های گران‌قیمت‌ِ پرتعداد نباید فیِ آخر را از اصلش بالا ببرد
        rows = [(999, 100), (2, 1)]
        parts = allocate_discount(rows, 7)
        self.assertEqual(totals(parts), 999 * 100 + 2 - 7)
        for (price, _), pieces in zip(rows, parts):
            self.assertTrue(all(0 <= new_price <= price for new_price, _ in pieces))

    def test_quantities_are_preserved_per_row(self):
        rows = [(1234, 7), (999, 3), (55555, 1)]
        for pieces, (_, quantity) in zip(allocate_discount(rows, 4321), rows):
            self.assertEqual(sum(q for _, q in pieces), quantity)

    def test_randomised_invariants(self):
        """ ۳٬۰۰۰ سبد تصادفی (بذر ثابت): جمع دقیق، فیِ هیچ ردیفی بیشتر از اصل/منفی نیست، حداکثر دو بخش برای هر ردیف """
        rng = random.Random(20260920)
        for _ in range(3000):
            rows = [(rng.choice([1, 7, 999, 1000, 33333, 124999, 5_000_000, rng.randint(1, 2_000_000)]), rng.randint(1, 40))
                    for _ in range(rng.randint(1, 6))]
            subtotal = sum(p * q for p, q in rows)
            discount = rng.choice([0, 1, subtotal // 3, subtotal - 1, subtotal, subtotal + 5, rng.randint(0, subtotal)])
            parts = allocate_discount(rows, discount)
            self.assertEqual(totals(parts), subtotal - max(0, min(discount, subtotal)), (rows, discount))
            for (price, quantity), pieces in zip(rows, parts):
                self.assertEqual(sum(q for _, q in pieces), quantity)
                self.assertLessEqual(len(pieces), 2)
                self.assertTrue(all(0 <= new_price <= price for new_price, _ in pieces), (rows, discount, pieces))

    def test_deterministic_for_retries(self):
        rows = [(1234, 3), (999, 5), (777, 2)]
        self.assertEqual(allocate_discount(rows, 1500), allocate_discount(rows, 1500))


class HolooInvoiceDiscountTests(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(phone_number='09120000760', erp_code='CUST-760')
        category = Category.objects.create(name='تست', slug='holoo-alloc-cat')
        self.products = [
            Product.objects.create(name=f'کالا {i}', slug=f'holoo-alloc-p{i}', erp_code=f'ERP-AL-{i}', category=category,
                                   price=price, stock=50)
            for i, price in enumerate((100000, 33333, 1999), start=1)
        ]

    def order(self, quantities=(2, 3, 7), order_discount=0, label='', shipping_cost=45000, method='courier', promotion=None):
        prices = (100000, 33333, 1999)
        items_sum = sum(p * q for p, q in zip(prices, quantities))
        shipping = shipping_cost if method == 'courier' else 0
        order = Order.objects.create(
            user=self.user, first_name='مریم', last_name='کاظمی', phone='09123334455', payment_method='cash',
            address='بلوار پردیسان', province='قم', city='قم', zone='پردیسان', shipping_method=method,
            shipping_label='ارسال با پیک', shipping_cost=shipping, order_discount=order_discount,
            order_discount_label=label, promotion_discount=promotion or 0,
            total_price=items_sum - order_discount + shipping,
        )
        for product, price, quantity in zip(self.products, prices, quantities):
            OrderItem.objects.create(order=order, product=product, price=price, quantity=quantity)
        return order

    def lines(self, order):
        rows = [(item, item.product.erp_code) for item in order.items.select_related('product').order_by('pk')]
        return item_lines(order, rows, 'ثبت از سایت - روش cash')

    def test_without_an_order_discount_lines_are_exactly_the_stored_prices(self):
        order = self.order()
        self.assertEqual([(l['ErpCode'], l['Amount'], l['Price']) for l in self.lines(order)],
                         [('ERP-AL-1', 2, 100000.0), ('ERP-AL-2', 3, 33333.0), ('ERP-AL-3', 7, 1999.0)])

    def test_invoice_sum_equals_the_payable_amount_for_many_discounts(self):
        for discount in (1, 999, 25000, 123457, 299998):
            with self.subTest(discount=discount):
                order = self.order(order_discount=discount, label='کوپن آزمون')
                payload = build_invoice_payload(order, self.lines(order), 'SHIP-1')
                self.assertEqual(payload_total(payload), order.total_price)

    def test_each_line_is_a_whole_number_and_never_above_the_stored_price(self):
        order = self.order(order_discount=123457)
        originals = {item.product.erp_code: item.price for item in order.items.select_related('product')}
        for line in self.lines(order):
            self.assertEqual(line['Price'], int(line['Price']))
            self.assertLessEqual(Decimal(str(line['Price'])), originals[line['ErpCode']])
            self.assertGreaterEqual(line['Price'], 0)

    def test_post_collect_order_has_no_shipping_row_but_the_sum_still_matches(self):
        order = self.order(order_discount=50000, method='post')
        payload = build_invoice_payload(order, self.lines(order), 'SHIP-1')
        self.assertNotIn('SHIP-1', [row['ErpCode'] for row in payload['Items']])
        self.assertEqual(payload_total(payload), order.total_price)

    def test_comment_names_the_order_level_discount(self):
        order = self.order(order_discount=25000, label='کوپن یلدا')
        self.assertIn('کوپن یلدا: 25000 (پخش‌شده روی فی اقلام)', invoice_comment(order))

    def test_comment_uses_a_generic_title_when_the_label_is_empty(self):
        self.assertIn('تخفیف سفارش: 25000', invoice_comment(self.order(order_discount=25000)))

    def test_comment_of_an_undiscounted_order_is_unchanged(self):
        comment = invoice_comment(self.order())
        self.assertNotIn('تخفیف', comment)

    def test_automatic_promotion_discount_is_not_spread_again(self):
        """ تخفیف خودکار از قبل در OrderItem.price است؛ فقط order_discount پخش می‌شود """
        order = self.order(promotion=40000)
        self.assertEqual([l['Price'] for l in self.lines(order)], [100000.0, 33333.0, 1999.0])


class HolooTaskDiscountTests(TestCase):
    """ مسیر واقعی تسک send_order_to_holoo با کلاینت هلوی mock """

    def setUp(self):
        self.user = CustomUser.objects.create_user(phone_number='09120000761', erp_code='CUST-761')
        category = Category.objects.create(name='تست', slug='holoo-alloc-task-cat')
        self.a = Product.objects.create(name='الف', slug='holoo-task-a', erp_code='ERP-TA', category=category, price=100000, stock=9)
        self.b = Product.objects.create(name='ب', slug='holoo-task-b', erp_code='ERP-TB', category=category, price=33333, stock=9)

    def send(self, order_discount, total_price=None, label='کوپن یلدا'):
        items_sum = 100000 * 2 + 33333 * 3
        order = Order.objects.create(
            user=self.user, first_name='مریم', last_name='کاظمی', phone='09123334455', payment_method='check',
            address='بلوار', province='قم', city='قم', zone='پردیسان', shipping_method='courier', shipping_label='ارسال با پیک',
            shipping_cost=45000, order_discount=order_discount, order_discount_label=label,
            total_price=total_price if total_price is not None else items_sum - order_discount + 45000,
        )
        OrderItem.objects.create(order=order, product=self.a, price=100000, quantity=2)
        OrderItem.objects.create(order=order, product=self.b, price=33333, quantity=3)
        cache.delete('lock:holoo:invoice:%s' % order.id)
        with mock.patch('holoo.client.HolooClient.insert_invoice') as insert:
            insert.return_value = {'success': True, 'InvoiceCode': 'INV-DISC'}
            with self.assertNoLogs('holoo.tasks', level='ERROR') if total_price is None else mock.MagicMock():
                send_order_to_holoo(order.id)
        return order, insert.call_args[0][0]

    def test_discounted_order_reaches_holoo_balanced_with_the_customers_payment(self):
        order, payload = self.send(order_discount=77777)
        self.assertEqual(payload_total(payload), order.total_price)
        self.assertEqual(sum(1 for row in payload['Items'] if row['ErpCode'] == 'SHIP-COURIER' or row['Comment'].startswith('کرایه')), 1)
        self.assertIn('کوپن یلدا: 77777', payload['Comment'])

    def test_undiscounted_order_is_sent_exactly_as_before(self):
        order, payload = self.send(order_discount=0, label='')
        prices = [(row['ErpCode'], row['Amount'], row['Price']) for row in payload['Items'] if row['ErpCode'].startswith('ERP-T')]
        self.assertEqual(prices, [('ERP-TA', 2, 100000.0), ('ERP-TB', 3, 33333.0)])
        self.assertEqual(payload_total(payload), order.total_price)

    def test_a_mismatch_between_invoice_and_payable_is_logged_loudly_but_still_sent(self):
        with self.assertLogs('holoo.tasks', level='ERROR') as logs:
            order, payload = self.send(order_discount=0, total_price=999)                # مبلغ سفارش با ردیف‌ها نمی‌خواند
        self.assertTrue(any('مغایرت مبلغ فاکتور هلو' in message for message in logs.output))
        order.refresh_from_db()
        self.assertEqual(order.holoo_invoice_id, 'INV-DISC')                              # ارسال متوقف نشد
