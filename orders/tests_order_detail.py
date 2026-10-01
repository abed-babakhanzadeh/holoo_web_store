"""
صفحه‌ی «جزئیات سفارش» (orders.views.OrderFullDetailView + templates/orders/order_full_detail.html):
کارت گیرنده، آکاردئون تاریخچه‌ی تراکنش‌ها، مرسوله با نوار پیشرفت، کارت کالاها و فیلدهای لغو (canceled_at / cancel_reason).
"""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from orders.models import Order
from payments.models import Transaction
from products.models import ProductColor
from returns.tests import ReturnsTestMixin
from reviews.models import Review


class DetailBase(ReturnsTestMixin, TestCase):
    def setUp(self):
        self.user = self.make_user()
        self.category = self.make_category()
        self.product = self.make_product(self.category)
        self.client.force_login(self.user)

    def order(self, **overrides):
        order = self.make_order(self.user, **overrides)
        self.make_order_item(order, self.product)
        return order

    def transaction(self, order, status='success', **overrides):
        data = dict(user=self.user, order=order, amount=order.total_price, status=status,
                    authority=f'AUTH-DETAIL-{Transaction.objects.count() + 1}')
        data.update(overrides)
        return Transaction.objects.create(**data)

    def get(self, order):
        return self.client.get(reverse('orders:order_detail_full', args=[order.pk]))


class CancelFieldsTests(DetailBase):
    def test_canceled_at_is_set_once_on_the_cancel_transition(self):
        order = self.order(status='shipped')
        self.assertIsNone(order.canceled_at)
        order.status = 'canceled'
        order.save()
        order.refresh_from_db()
        first = order.canceled_at
        self.assertIsNotNone(first)
        order.cancel_reason = 'درخواست مشتری'
        order.save()
        order.refresh_from_db()
        self.assertEqual(order.canceled_at, first)                          # ذخیره‌ی بعدی تاریخ را عوض نمی‌کند

    def test_canceled_at_is_written_even_with_update_fields(self):
        order = self.order(status='shipped')
        order = Order.objects.get(pk=order.pk)
        order.status = 'canceled'
        order.save(update_fields=['status'])
        self.assertIsNotNone(Order.objects.get(pk=order.pk).canceled_at)

    def test_non_canceled_order_has_no_canceled_at(self):
        order = self.order()
        self.assertIsNone(order.canceled_at)
        self.assertEqual(order.cancel_reason, '')


class RecipientCardTests(DetailBase):
    def test_shows_recipient_order_and_payment_info(self):
        order = self.order(first_name='مریم', last_name='احمدی', phone='09123456789', total_price=250000,
                           province='تهران', city='تهران', address='خیابان ولیعصر', postal_code='1234567890')
        html = self.get(order).content.decode()
        for text in ('مریم احمدی', '09123456789', 'تهران، تهران، خیابان ولیعصر', '1234567890', f'#{order.pk}',
                     '250000', 'مشاهده فاکتور', 'ثبت درخواست مرجوعی'):
            self.assertIn(text, html)
        # تاریخ شمسی (۱۴۰۵ یا ۱۴۰۶)، نه میلادی
        self.assertRegex(html, r'14\d\d/\d\d/\d\d - \d\d:\d\d')

    def test_return_button_is_a_link_when_returnable_and_disabled_otherwise(self):
        order = self.order(delivered_at=timezone.now())
        Order.objects.filter(pk=order.pk).update(delivered_at=timezone.now())
        response = self.get(order)
        self.assertContains(response, reverse('returns:wizard_step1', args=[order.pk]))
        old = self.order(status='delivered')
        Order.objects.filter(pk=old.pk).update(delivered_at=timezone.now() - timedelta(days=30))
        response = self.get(old)
        self.assertNotContains(response, reverse('returns:wizard_step1', args=[old.pk]))

    def test_invoice_button_is_a_disabled_placeholder(self):
        html = self.get(self.order()).content.decode()
        self.assertRegex(html, r'<button type="button" class="od-btn" disabled title="[^"]+">\s*<svg[^>]*>.*?</svg>\s*مشاهده فاکتور')


class TransactionHistoryTests(DetailBase):
    def test_accordion_lists_transactions_newest_first_with_ref_id_and_green_status(self):
        order = self.order(total_price=300000)
        failed = self.transaction(order, status='failed')
        ok = self.transaction(order, status='success', ref_id='REF-998877')
        Transaction.objects.filter(pk=failed.pk).update(created_at=timezone.now() - timedelta(hours=2))
        html = self.get(order).content.decode()
        self.assertIn('<details class="od-card od-acc">', html)
        self.assertIn('تاریخچه تراکنش‌ها', html)
        self.assertIn('REF-998877', html)
        self.assertIn('od-tx-status is-ok', html)
        self.assertIn('od-tx-status is-fail', html)
        self.assertLess(html.index('REF-998877'), html.index('od-tx-status is-fail'))      # موفق (جدیدتر) بالاتر از ناموفق
        self.assertIn('پرداخت موفق', html)
        self.assertIn('پرداخت ناموفق', html)

    def test_wallet_share_and_total_amount_are_shown_for_mixed_payment(self):
        order = self.order(total_price=300000)
        self.transaction(order, amount=Decimal('200000'), wallet_amount=Decimal('100000'), ref_id='REF-MIX')
        html = self.get(order).content.decode()
        self.assertIn('300000', html)                                                      # total_amount = درگاه + کیف‌پول
        self.assertIn('سهم کیف‌پول', html)
        self.assertIn('100000', html)

    def test_no_accordion_without_transactions(self):
        html = self.get(self.order(status='pending')).content.decode()
        self.assertNotIn('تاریخچه تراکنش‌ها', html)

    def test_only_own_transactions_are_listed(self):
        order = self.order()
        other_user = self.make_user('09140007777')
        other_order = self.make_order(other_user)
        Transaction.objects.create(user=other_user, order=other_order, amount=1, authority='AUTH-OTHER', status='success', ref_id='REF-FOREIGN')
        self.transaction(order, ref_id='REF-MINE')
        html = self.get(order).content.decode()
        self.assertIn('REF-MINE', html)
        self.assertNotIn('REF-FOREIGN', html)


class ShipmentCardTests(DetailBase):
    def test_delivered_shipment_has_full_green_bar_and_delivery_date(self):
        order = self.order(status='delivered', tracking_code='TRK-123456', shipping_method='courier', shipping_cost=45000)
        Order.objects.filter(pk=order.pk).update(delivered_at=timezone.now())
        self.transaction(order)
        html = self.get(order).content.decode()
        self.assertIn('od-progress is-done', html)
        self.assertIn('aria-valuenow="100"', html)
        self.assertIn('width: 100%', html)
        self.assertIn('TRK-123456', html)
        self.assertIn('45000', html)
        self.assertIn('مرحله 5 از 5', html)

    def test_shipped_order_bar_is_partial_and_not_marked_done(self):
        order = self.order(status='shipped')
        self.transaction(order)
        html = self.get(order).content.decode()
        self.assertNotIn('od-progress is-done', html)
        self.assertIn('aria-valuenow="75"', html)
        self.assertIn('ارسال شده', html)

    def test_canceled_order_shows_banner_with_date_and_reason_and_no_progress_bar(self):
        order = self.order(status='shipped')
        order = Order.objects.get(pk=order.pk)
        order.status = 'canceled'
        order.cancel_reason = 'موجودی کالا تمام شد'
        order.save()
        html = self.get(order).content.decode()
        self.assertIn('این سفارش لغو شده است', html)
        self.assertIn('دلیل لغو: موجودی کالا تمام شد', html)
        self.assertRegex(html, r'لغو شده است · 14\d\d/\d\d/\d\d \d\d:\d\d')
        self.assertNotIn('role="progressbar"', html)

    def test_legacy_canceled_order_without_date_still_renders(self):
        order = self.order(status='canceled')
        Order.objects.filter(pk=order.pk).update(canceled_at=None, cancel_reason='')
        response = self.get(order)
        self.assertContains(response, 'این سفارش لغو شده است')
        self.assertNotContains(response, 'دلیل لغو')


class ItemCardTests(DetailBase):
    def test_item_card_shows_name_color_quantity_price_and_image(self):
        color = ProductColor.objects.create(product=self.product, name='آبی نفتی', hex_code='#123456')
        order = self.make_order(self.user)
        item = self.make_order_item(order, self.product, quantity=3, price=100000)
        item.color = color
        item.save()
        html = self.get(order).content.decode()
        for text in ('کالای تست', 'آبی نفتی', '#123456', 'تعداد: <b>3</b>', '100000', '300000', self.product.main_image_url):
            self.assertIn(text, html)

    def test_rating_section_only_after_delivery(self):
        delivered = self.order(status='delivered')
        self.transaction(delivered)
        html = self.get(delivered).content.decode()
        self.assertEqual(html.count('class="od-star '), 5)
        self.assertIn('ثبت دیدگاه', html)
        self.assertIn('data-review-trigger', html)
        self.assertIn(f'{reverse("products:product_detail", args=[self.product.slug])}#Comments', html)
        shipped = self.order(status='shipped')
        self.transaction(shipped)
        html = self.get(shipped).content.decode()
        self.assertNotIn('od-stars', html)
        self.assertNotIn('data-review-trigger', html)

    def test_existing_review_fills_stars_and_offers_edit(self):
        order = self.order(status='delivered')
        self.transaction(order)
        review = Review.objects.create(product=self.product, user=self.user, rating=4, body='خوب', status='published')
        # پاسخِ همین کاربر نباید جای نظر اصلی را بگیرد
        Review.objects.create(product=self.product, user=self.user, parent=review, body='پاسخ', status='published')
        html = self.get(order).content.decode()
        self.assertEqual(html.count('od-star is-on'), 4)
        self.assertIn('ویرایش دیدگاه', html)
        self.assertIn(reverse('reviews:edit', args=[review.id]), html)
        self.assertIn('دیدگاه شما ثبت شده است', html)

    def test_inactive_product_has_no_rating_section(self):
        order = self.order(status='delivered')
        self.transaction(order)
        self.product.is_active = False
        self.product.save()
        self.assertNotContains(self.get(order), 'od-stars')


class QueryCountTests(DetailBase):
    def _build(self, items, transactions):
        order = self.make_order(self.user)
        for index in range(items):
            product = self.make_product(self.category, name=f'کالا {index}', slug=f'qc-{order.pk}-{index}', erp_code=f'ERP-QC-{order.pk}-{index}')
            self.make_order_item(order, product)
        for _ in range(transactions):
            self.transaction(order, status='failed')
        self.transaction(order)
        return order

    def test_query_count_does_not_grow_with_items_or_transactions(self):
        small = self._build(1, 0)
        big = self._build(6, 5)
        self.get(small)                                                                    # گرم‌کردن (کش تنظیمات سایت و ...)
        with self.assertNumQueries(self._count(small)):
            self.get(big)

    def _count(self, order):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as ctx:
            self.get(order)
        return len(ctx)

    def test_is_paid_uses_prefetched_transactions(self):
        order = self.order()
        self.transaction(order)
        order = Order.objects.prefetch_related('transactions').get(pk=order.pk)
        with self.assertNumQueries(0):
            self.assertTrue(order.is_paid)
