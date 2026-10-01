"""
دکمه‌ی «ثبت نظر درباره محصولات» در لیست سفارش‌ها و جزئیات سفارش: فقط وقتی سفارش «تحویل داده شده» باشد فعال
است و به صفحه‌ی ثبت نظر کالاهای همان سفارش می‌رود (orders.views.OrderReviewsView).
"""
from django.test import TestCase
from django.urls import reverse

from payments.models import Transaction
from products.models import Product
from returns.tests import ReturnsTestMixin
from reviews.models import Review


class OrderReviewsBase(ReturnsTestMixin, TestCase):
    def setUp(self):
        self.user = self.make_user()
        self.category = self.make_category()
        self.product_a = self.make_product(self.category)
        self.product_b = self.make_product(self.category, name='کالای دوم', slug='review-second', erp_code='ERP-REV-2')
        self.client.force_login(self.user)

    def delivered_order(self, paid=True, **overrides):
        order = self.make_order(self.user, **overrides)
        self.make_order_item(order, self.product_a)
        self.make_order_item(order, self.product_b)
        if paid:
            Transaction.objects.create(user=self.user, order=order, amount=order.total_price,
                                       authority=f'AUTH-REV-{order.pk}', status='success')
        return order

    def url(self, order):
        return reverse('orders:order_reviews', args=[order.pk])


class ButtonStateTests(OrderReviewsBase):
    def test_can_review_only_for_delivered_orders(self):
        self.assertTrue(self.delivered_order().can_review)
        self.assertFalse(self.delivered_order(status='shipped').can_review)
        self.assertFalse(self.delivered_order(status='canceled').can_review)
        self.assertFalse(self.delivered_order(paid=False).can_review)         # در انتظار پرداخت، حتی اگر status=delivered

    def test_detail_page_button_is_active_link_when_delivered(self):
        order = self.delivered_order()
        response = self.client.get(reverse('orders:order_detail_full', args=[order.pk]))
        self.assertContains(response, f'href="{self.url(order)}"')
        self.assertNotContains(response, 'پس از تحویل سفارش می‌توانید برای کالاها نظر ثبت کنید')

    def test_detail_page_button_is_disabled_before_delivery(self):
        order = self.delivered_order(status='shipped')
        response = self.client.get(reverse('orders:order_detail_full', args=[order.pk]))
        self.assertNotContains(response, f'href="{self.url(order)}"')
        self.assertContains(response, 'پس از تحویل سفارش می‌توانید برای کالاها نظر ثبت کنید')

    def test_history_list_shows_active_button_for_delivered_and_disabled_for_others(self):
        delivered = self.delivered_order()
        shipped = self.delivered_order(status='shipped')
        response = self.client.get(reverse('orders:order_history'))
        self.assertContains(response, f'href="{self.url(delivered)}"')
        self.assertNotContains(response, f'href="{self.url(shipped)}"')
        self.assertContains(response, 'پس از تحویل سفارش می‌توانید برای کالاها نظر ثبت کنید')

    def test_inline_order_detail_partial_links_to_reviews_when_delivered(self):
        order = self.delivered_order()
        response = self.client.get(reverse('orders:order_detail', args=[order.pk]))
        self.assertContains(response, f'href="{self.url(order)}"')
        shipped = self.delivered_order(status='shipped')
        response = self.client.get(reverse('orders:order_detail', args=[shipped.pk]))
        self.assertNotContains(response, f'href="{self.url(shipped)}"')


class ReviewsPageTests(OrderReviewsBase):
    def test_lists_each_product_once_with_review_links(self):
        order = self.delivered_order()
        self.make_order_item(order, self.product_a, quantity=1)               # همان کالا دوباره: تکرار نمایش داده نشود
        response = self.client.get(self.url(order))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'کالای تست', count=2)                    # alt تصویر + نام
        self.assertContains(response, 'کالای دوم')
        for product in (self.product_a, self.product_b):
            self.assertContains(response, f'href="{reverse("products:product_detail", args=[product.slug])}#Comments"')
        self.assertContains(response, 'برای 0 از 2 کالا نظر ثبت کرده‌اید')

    def test_shows_existing_review_state_and_edit_link(self):
        order = self.delivered_order()
        review = Review.objects.create(product=self.product_a, user=self.user, rating=4, body='خوب بود', status='pending')
        rejected = Review.objects.create(product=self.product_b, user=self.user, rating=2, body='بد', status='rejected',
                                         rejection_reason='محتوای نامناسب')
        response = self.client.get(self.url(order))
        self.assertContains(response, 'در انتظار تأیید')
        self.assertContains(response, 'رد شد')
        self.assertContains(response, 'دلیل رد: محتوای نامناسب')
        self.assertContains(response, f'href="{reverse("reviews:edit", args=[review.id])}"')
        self.assertContains(response, f'href="{reverse("reviews:edit", args=[rejected.id])}"')
        self.assertContains(response, 'برای 2 از 2 کالا نظر ثبت کرده‌اید')

    def test_replies_are_not_counted_as_the_users_review(self):
        order = self.delivered_order()
        parent = Review.objects.create(product=self.product_a, user=self.make_user('09140009999'), rating=5,
                                       body='عالی', status='published')
        Review.objects.create(product=self.product_a, user=self.user, parent=parent, body='موافقم', status='published')
        response = self.client.get(self.url(order))
        self.assertContains(response, 'برای 0 از 2 کالا نظر ثبت کرده‌اید')

    def test_unavailable_product_has_no_review_button(self):
        order = self.delivered_order()
        Product.objects.filter(pk=self.product_b.pk).update(is_active=False)
        response = self.client.get(self.url(order))
        self.assertContains(response, 'در دسترس نیست')
        self.assertNotContains(response, f'href="{reverse("products:product_detail", args=[self.product_b.slug])}#Comments"')

    def test_not_delivered_order_redirects_back_with_a_message(self):
        order = self.delivered_order(status='shipped')
        response = self.client.get(self.url(order), follow=True)
        self.assertRedirects(response, reverse('orders:order_detail_full', args=[order.pk]))
        self.assertContains(response, 'ثبت نظر پس از تحویل سفارش امکان‌پذیر است.')

    def test_other_users_order_is_404_and_anonymous_is_redirected(self):
        other = self.make_user('09140008888')
        other_order = self.make_order(other)
        self.make_order_item(other_order, self.product_a)
        self.assertEqual(self.client.get(self.url(other_order)).status_code, 404)
        self.client.logout()
        response = self.client.get(self.url(other_order))
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response.url)

    def test_review_flow_end_to_end_from_the_page_link(self):
        """ لینک «ثبت نظر» صفحه را به محصول می‌برد و نظر ثبت‌شده برچسب «خریدار تاییدشده» می‌گیرد """
        order = self.delivered_order()
        response = self.client.post(reverse('reviews:create', args=[self.product_a.slug]),
                                    {'rating': '5', 'body': 'کیفیت عالی'})
        self.assertEqual(response.status_code, 302)
        review = Review.objects.get(product=self.product_a, user=self.user)
        self.assertTrue(review.is_verified_purchase)
        page = self.client.get(self.url(order))
        self.assertContains(page, 'برای 1 از 2 کالا نظر ثبت کرده‌اید')
