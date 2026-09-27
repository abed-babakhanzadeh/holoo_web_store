"""
تست‌های returns/refund_calculator.py (Phase 1 - Part B): تسهیم کوپن با allocate_discount،
ظرفیت قابل‌مرجوع، و تشخیص «مرجوعی کامل سفارش» برای استرداد کرایه.
"""

from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from returns.models import ReturnItem, ReturnRequest
from returns.refund_calculator import (
    calculate_item_refund_amount, calculate_shipping_refund, get_order_item_allocated_unit_price,
    get_returnable_quantity, is_full_order_return,
)
from returns.tests import ReturnsTestMixin


class AllocatedUnitPriceTests(ReturnsTestMixin, TestCase):
    def setUp(self):
        self.user = self.make_user()
        self.category = self.make_category()

    def test_no_discount_returns_the_item_price_unchanged(self):
        order = self.make_order(self.user, order_discount=0)
        product = self.make_product(self.category, name='کالای بدون تخفیف', slug='no-discount-product', erp_code='ERP-NODISC-1')
        item = self.make_order_item(order, product, price=50000, quantity=2)
        self.assertEqual(get_order_item_allocated_unit_price(item), Decimal(50000))

    def test_discount_is_allocated_proportionally_with_exact_rial_precision(self):
        """
        سه‌سطری با remainder غیرصفر - مقادیر دستی محاسبه‌شده تا با پیاده‌سازیِ allocate_discount
        (تست‌شده در holoo/tests) دقیقاً یک‌سان بمانند: subtotal=37000, discount=10000 -> target=27000.
        """
        order = self.make_order(self.user, order_discount=10000)
        product_a = self.make_product(self.category, name='کالای آ', slug='alloc-product-a', erp_code='ERP-ALLOC-A')
        product_b = self.make_product(self.category, name='کالای ب', slug='alloc-product-b', erp_code='ERP-ALLOC-B')
        item_a = self.make_order_item(order, product_a, price=10000, quantity=3)   # subtotal 30000
        item_b = self.make_order_item(order, product_b, price=7000, quantity=1)    # subtotal 7000

        self.assertEqual(get_order_item_allocated_unit_price(item_a), Decimal(7297))
        self.assertEqual(get_order_item_allocated_unit_price(item_b), Decimal(5109))
        # هیچ ریالی گم نشود: جمع دو ردیف باید دقیقاً با subtotal-discount برابر باشد
        total_allocated = get_order_item_allocated_unit_price(item_a) * 3 + get_order_item_allocated_unit_price(item_b) * 1
        self.assertEqual(total_allocated, 27000)

    def test_intra_row_rounding_never_overcharges_the_customer(self):
        """
        وقتی باقی‌مانده‌ی گردکردن داخل خودِ یک ردیف بین چند واحد پخش می‌شود (نه لزوماً مقسوم‌علیه
        دقیق تعداد)، فیِ واحدِ گزارش‌شده (تقسیم صحیح رو به پایین) هرگز از مبلغ واقعیِ تخصیص‌یافته
        به آن ردیف بیشتر نمی‌شود - حداکثر چند ریال (کمتر از تعداد) کمتر گزارش می‌شود، نه بیشتر.
        """
        order = self.make_order(self.user, order_discount=1)
        product = self.make_product(self.category, name='کالای تک‌ردیفی', slug='intra-row-product', erp_code='ERP-INTRAROW-1')
        item = self.make_order_item(order, product, price=10000, quantity=3)   # subtotal 30000, discount=1 -> target=29999

        unit_price = get_order_item_allocated_unit_price(item)
        self.assertEqual(unit_price, Decimal(9999))
        self.assertLessEqual(unit_price * item.quantity, 29999)   # هرگز بیشتر از سهمِ واقعی این ردیف

    def test_belongs_to_a_different_order_raises(self):
        order1 = self.make_order(self.user)
        order2 = self.make_order(self.user)
        product = self.make_product(self.category, name='محصول مستقل', slug='foreign-order-product', erp_code='ERP-FOREIGN-1')
        item_of_order2 = self.make_order_item(order2, product, price=10000, quantity=1)
        item_of_order2.order_id = order1.id   # وضعیت ناسازگار دستی (نباید در عمل رخ دهد)
        with self.assertRaises(ValueError):
            get_order_item_allocated_unit_price(item_of_order2)


class CalculateItemRefundAmountTests(ReturnsTestMixin, TestCase):
    def setUp(self):
        self.user = self.make_user()
        self.category = self.make_category()
        self.order = self.make_order(self.user, order_discount=0)
        self.product = self.make_product(self.category)
        self.order_item = self.make_order_item(self.order, self.product, price=50000, quantity=4)
        self.reason = self.make_reason()
        self.request = self.make_return_request(self.order, self.user)

    def make_return_item(self, requested_quantity, approved_quantity=None):
        return ReturnItem.objects.create(
            return_request=self.request, order_item=self.order_item, reason=self.reason,
            requested_quantity=requested_quantity, approved_quantity=approved_quantity,
        )

    def test_uses_approved_quantity_not_requested_quantity(self):
        item = self.make_return_item(requested_quantity=3, approved_quantity=1)
        self.assertEqual(calculate_item_refund_amount(item), Decimal(50000))

    def test_zero_approved_quantity_means_zero_refund(self):
        item = self.make_return_item(requested_quantity=3, approved_quantity=0)
        self.assertEqual(calculate_item_refund_amount(item), Decimal('0'))

    def test_null_approved_quantity_means_zero_refund(self):
        """ قبل از بازرسی (approved_quantity هنوز خالی) هیچ مبلغی محاسبه نمی‌شود """
        item = self.make_return_item(requested_quantity=3, approved_quantity=None)
        self.assertEqual(calculate_item_refund_amount(item), Decimal('0'))


class GetReturnableQuantityTests(ReturnsTestMixin, TestCase):
    def setUp(self):
        self.user = self.make_user()
        self.category = self.make_category()
        self.order = self.make_order(self.user)
        self.product = self.make_product(self.category)
        self.order_item = self.make_order_item(self.order, self.product, price=10000, quantity=5)
        self.reason = self.make_reason()

    def test_full_quantity_returnable_with_no_return_requests(self):
        self.assertEqual(get_returnable_quantity(self.order_item), 5)

    def test_pending_request_locks_by_requested_quantity(self):
        request = self.make_return_request(self.order, self.user, status=ReturnRequest.STATUS_PENDING)
        ReturnItem.objects.create(
            return_request=request, order_item=self.order_item, reason=self.reason, requested_quantity=2,
        )
        self.assertEqual(get_returnable_quantity(self.order_item), 3)

    def test_approved_request_still_locks_by_requested_quantity(self):
        request = self.make_return_request(self.order, self.user, status=ReturnRequest.STATUS_APPROVED)
        ReturnItem.objects.create(
            return_request=request, order_item=self.order_item, reason=self.reason, requested_quantity=2,
        )
        self.assertEqual(get_returnable_quantity(self.order_item), 3)

    def test_item_received_switches_to_approved_quantity_and_releases_rejected_portion(self):
        """ کاربر ۲ واحد خواسته بود، کارشناس فقط ۱ واحد را تأیید کرد - ۱ واحد فوراً آزاد می‌شود """
        request = self.make_return_request(self.order, self.user, status=ReturnRequest.STATUS_ITEM_RECEIVED)
        ReturnItem.objects.create(
            return_request=request, order_item=self.order_item, reason=self.reason,
            requested_quantity=2, approved_quantity=1,
        )
        self.assertEqual(get_returnable_quantity(self.order_item), 4)

    def test_refund_pending_and_completed_also_use_approved_quantity(self):
        for status in (ReturnRequest.STATUS_REFUND_PENDING, ReturnRequest.STATUS_COMPLETED):
            with self.subTest(status=status):
                order_item = self.make_order_item(
                    self.order, self.make_product(self.category, name=f'کالای {status}', slug=f'qty-test-{status.lower()}', erp_code=f'ERP-QTY-{status}'),
                    price=10000, quantity=5,
                )
                request = self.make_return_request(
                    self.order, self.user, status=status,
                    completed_at=timezone.now() if status == ReturnRequest.STATUS_COMPLETED else None,
                )
                ReturnItem.objects.create(
                    return_request=request, order_item=order_item, reason=self.reason,
                    requested_quantity=3, approved_quantity=2,
                )
                self.assertEqual(get_returnable_quantity(order_item), 3)

    def test_rejected_request_locks_nothing_regardless_of_quantities(self):
        request = self.make_return_request(
            self.order, self.user, status=ReturnRequest.STATUS_REJECTED, rejection_reason='کالا استفاده‌شده بود',
        )
        ReturnItem.objects.create(
            return_request=request, order_item=self.order_item, reason=self.reason,
            requested_quantity=5, approved_quantity=0,
        )
        self.assertEqual(get_returnable_quantity(self.order_item), 5)

    def test_multiple_active_requests_accumulate(self):
        r1 = self.make_return_request(self.order, self.user, status=ReturnRequest.STATUS_PENDING)
        ReturnItem.objects.create(return_request=r1, order_item=self.order_item, reason=self.reason, requested_quantity=1)
        r2 = self.make_return_request(self.order, self.user, status=ReturnRequest.STATUS_ITEM_RECEIVED)
        ReturnItem.objects.create(
            return_request=r2, order_item=self.order_item, reason=self.reason, requested_quantity=2, approved_quantity=2,
        )
        self.assertEqual(get_returnable_quantity(self.order_item), 2)


class FullOrderReturnAndShippingRefundTests(ReturnsTestMixin, TestCase):
    def setUp(self):
        self.user = self.make_user()
        self.category = self.make_category()
        self.order = self.make_order(self.user, shipping_cost=25000)
        self.product_a = self.make_product(self.category, name='کالای شیپینگ آ', slug='shipping-product-a', erp_code='ERP-SHIP-A')
        self.product_b = self.make_product(self.category, name='کالای شیپینگ ب', slug='shipping-product-b', erp_code='ERP-SHIP-B')
        self.item_a = self.make_order_item(self.order, self.product_a, price=10000, quantity=2)
        self.item_b = self.make_order_item(self.order, self.product_b, price=20000, quantity=1)
        self.reason = self.make_reason()

    def _completed_request(self, order_item, approved_quantity):
        request = self.make_return_request(
            self.order, self.user, status=ReturnRequest.STATUS_COMPLETED, completed_at=timezone.now(),
        )
        ReturnItem.objects.create(
            return_request=request, order_item=order_item, reason=self.reason,
            requested_quantity=approved_quantity, approved_quantity=approved_quantity,
        )
        return request

    def test_partial_return_is_not_a_full_order_return(self):
        self._completed_request(self.item_a, approved_quantity=1)   # فقط ۱ از ۲ واحد کالای آ
        self.assertFalse(is_full_order_return(self.order))
        self.assertEqual(calculate_shipping_refund(self.order), Decimal('0'))

    def test_full_return_across_multiple_requests_triggers_shipping_refund(self):
        self._completed_request(self.item_a, approved_quantity=2)   # کل کالای آ
        self._completed_request(self.item_b, approved_quantity=1)   # کل کالای ب
        self.assertTrue(is_full_order_return(self.order))
        self.assertEqual(calculate_shipping_refund(self.order), Decimal(25000))

    def test_shipping_already_refunded_on_another_request_is_not_refunded_twice(self):
        self._completed_request(self.item_a, approved_quantity=2)
        second = self._completed_request(self.item_b, approved_quantity=1)
        ReturnRequest.objects.filter(pk=second.pk).update(shipping_refunded=True, shipping_refund_amount=25000)

        third = self.make_return_request(self.order, self.user, status=ReturnRequest.STATUS_ITEM_RECEIVED)
        self.assertEqual(calculate_shipping_refund(self.order, exclude_return_request=third), Decimal('0'))

    def test_zero_shipping_cost_order_never_triggers_a_refund(self):
        order = self.make_order(self.user, shipping_cost=0)
        product = self.make_product(self.category, name='کالای بدون کرایه', slug='zero-shipping-product', erp_code='ERP-ZEROSHIP-1')
        item = self.make_order_item(order, product, price=10000, quantity=1)
        request = self.make_return_request(order, self.user, status=ReturnRequest.STATUS_COMPLETED, completed_at=timezone.now())
        ReturnItem.objects.create(return_request=request, order_item=item, reason=self.reason, requested_quantity=1, approved_quantity=1)
        self.assertTrue(is_full_order_return(order))
        self.assertEqual(calculate_shipping_refund(order), Decimal('0'))
