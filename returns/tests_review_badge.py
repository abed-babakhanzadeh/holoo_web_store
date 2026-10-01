"""
نشان «خریدار» برای نظر کسی که کالا را مرجوع کرده (reviews.purchases.returned_buyer_ids + returns/purchases.py):
نشان حذف نمی‌شود، با برچسب «خریدار این محصول (مرجوع شده)» نمایش داده می‌شود.
"""
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from payments.models import Transaction
from returns.models import ReturnItem, ReturnRequest
from returns.tests import ReturnsTestMixin
from reviews.models import Review
from reviews.purchases import returned_buyer_ids

RETURNED_LABEL = 'خریدار این محصول (مرجوع شده)'
BUYER_LABEL = '>خریدار</span>'


class ReturnedBadgeBase(ReturnsTestMixin, TestCase):
    def setUp(self):
        self.user = self.make_user()
        self.category = self.make_category()
        self.product = self.make_product(self.category)
        self.reason = self.make_reason()
        self.order = self.make_order(self.user)
        self.item = self.make_order_item(self.order, self.product)
        Transaction.objects.create(user=self.user, order=self.order, amount=self.order.total_price,
                                   authority='AUTH-BADGE', status='success')
        self.review = Review.objects.create(product=self.product, user=self.user, rating=4, body='تجربه‌ی من',
                                            status='published', is_verified_purchase=True)

    def make_return(self, status, approved_quantity=None, user=None, item=None):
        extra = {}
        if status == ReturnRequest.STATUS_REJECTED:
            extra['rejection_reason'] = 'خارج از شرایط'
        if status == ReturnRequest.STATUS_COMPLETED:
            extra['completed_at'] = timezone.now()
        request = self.make_return_request(self.order, user or self.user, status=status, **extra)
        ReturnItem.objects.create(return_request=request, order_item=item or self.item, reason=self.reason,
                                  requested_quantity=1, approved_quantity=approved_quantity)
        return request

    def page(self):
        return self.client.get(reverse('products:product_detail', args=[self.product.slug])).content.decode()


class ReturnedProviderTests(ReturnedBadgeBase):
    def test_item_received_refund_pending_and_completed_count_as_returned(self):
        for status in (ReturnRequest.STATUS_ITEM_RECEIVED, ReturnRequest.STATUS_REFUND_PENDING, ReturnRequest.STATUS_COMPLETED):
            with self.subTest(status=status):
                ReturnRequest.objects.all().delete()
                self.make_return(status)
                self.assertEqual(returned_buyer_ids(self.product, [self.user.pk]), {self.user.pk})

    def test_pending_approved_and_rejected_requests_are_not_returned_yet(self):
        for status in (ReturnRequest.STATUS_PENDING, ReturnRequest.STATUS_APPROVED, ReturnRequest.STATUS_REJECTED):
            with self.subTest(status=status):
                ReturnRequest.objects.all().delete()
                self.make_return(status)
                self.assertEqual(returned_buyer_ids(self.product, [self.user.pk]), set())

    def test_zero_approved_quantity_after_inspection_is_not_returned(self):
        self.make_return(ReturnRequest.STATUS_COMPLETED, approved_quantity=0)
        self.assertEqual(returned_buyer_ids(self.product, [self.user.pk]), set())

    def test_other_users_and_other_products_are_ignored(self):
        other = self.make_user('09140002222')
        other_order = self.make_order(other)
        other_item = self.make_order_item(other_order, self.product)
        request = self.make_return_request(other_order, other, status=ReturnRequest.STATUS_COMPLETED, completed_at=timezone.now())
        ReturnItem.objects.create(return_request=request, order_item=other_item, reason=self.reason, requested_quantity=1)
        self.assertEqual(returned_buyer_ids(self.product, [self.user.pk]), set())
        self.assertEqual(returned_buyer_ids(self.product, [self.user.pk, other.pk]), {other.pk})
        second = self.make_product(self.category, name='کالای دیگر', slug='badge-other', erp_code='ERP-BADGE-2')
        self.assertEqual(returned_buyer_ids(second, [other.pk]), set())

    def test_empty_user_ids_makes_no_query(self):
        with self.assertNumQueries(0):
            self.assertEqual(returned_buyer_ids(self.product, []), set())


class ReturnedBadgePageTests(ReturnedBadgeBase):
    def test_normal_verified_buyer_keeps_the_plain_badge(self):
        html = self.page()
        self.assertIn(BUYER_LABEL, html)
        self.assertNotIn(RETURNED_LABEL, html)

    def test_returned_buyer_badge_is_kept_with_the_returned_label(self):
        self.make_return(ReturnRequest.STATUS_COMPLETED)
        html = self.page()
        self.assertIn(RETURNED_LABEL, html)
        self.assertNotIn(BUYER_LABEL, html)                                               # برچسب ساده جایگزین شده، دوبار نیامده

    def test_in_progress_return_does_not_change_the_badge(self):
        self.make_return(ReturnRequest.STATUS_PENDING)
        html = self.page()
        self.assertIn(BUYER_LABEL, html)
        self.assertNotIn(RETURNED_LABEL, html)

    def test_reply_by_a_returned_buyer_gets_no_badge(self):
        self.make_return(ReturnRequest.STATUS_COMPLETED)
        other = self.make_user('09140003333')
        Review.objects.create(product=self.product, user=self.user, parent=Review.objects.create(
            product=self.product, user=other, rating=5, body='نظر دیگران', status='published'),
            body='پاسخ من', status='published')
        self.assertEqual(self.page().count(RETURNED_LABEL), 1)                            # فقط روی نظر اصلی خودش

    def test_returning_does_not_unset_the_stored_verified_flag(self):
        self.make_return(ReturnRequest.STATUS_COMPLETED)
        self.page()
        self.review.refresh_from_db()
        self.assertTrue(self.review.is_verified_purchase)
