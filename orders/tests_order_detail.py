"""
صفحه‌ی «جزئیات سفارش» (orders.views.OrderFullDetailView + templates/orders/order_full_detail.html):
کارت گیرنده، آکاردئون تاریخچه‌ی تراکنش‌ها، مرسوله با نوار پیشرفت، کارت کالاها و فیلدهای لغو (canceled_at / cancel_reason).
"""
from datetime import timedelta
import base64
import json
import re
from html import unescape
import shutil
import tempfile
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from orders.models import Order
from payments.models import Transaction
from products.models import ProductColor
from returns.tests import ReturnsTestMixin
from reviews.models import Review, ReviewPoint


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
                     '250,000', 'مشاهده فاکتور', 'ثبت درخواست مرجوعی'):
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

    def test_invoice_button_links_to_the_invoice_for_invoiceable_orders_and_is_disabled_otherwise(self):
        html = self.get(self.order()).content.decode()                                    # تحویل‌شده
        self.assertRegex(html, r'<a href="/orders/history/\d+/invoice/" class="od-btn is-outline"')
        pending = self.order(status='pending')
        html = self.get(pending).content.decode()                                         # پرداخت‌نشده
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
        self.assertIn('300,000', html)                                                     # total_amount = درگاه + کیف‌پول
        self.assertIn('سهم کیف‌پول', html)
        self.assertIn('100,000', html)

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
    def test_delivered_shipment_has_full_green_bar_tick_and_no_next_step(self):
        order = self.order(status='delivered', tracking_code='TRK-123456', shipping_method='courier', shipping_cost=45000)
        Order.objects.filter(pk=order.pk).update(delivered_at=timezone.now())
        self.transaction(order)
        html = self.get(order).content.decode()
        self.assertIn('od-progress is-done', html)
        self.assertIn('aria-valuenow="100"', html)
        self.assertIn('width: 100%', html)
        self.assertIn('تحویل مرسوله به مشتری', html)
        self.assertNotIn('مرحله بعد', html)                                              # تحویل‌شده: مرحله‌ی بعد پنهان است
        self.assertRegex(html, r'تاریخ تحویل: <b>14\d\d/\d\d/\d\d</b>')
        self.assertIn('TRK-123456', html)
        self.assertIn('45,000', html)

    def test_shipped_order_shows_title_partial_bar_and_next_step(self):
        order = self.order(status='shipped')
        self.transaction(order)
        html = self.get(order).content.decode()
        self.assertNotIn('od-progress is-done', html)
        self.assertIn('aria-valuenow="75"', html)
        self.assertIn('ارسال شده / تحویل به پست', html)
        self.assertIn('مرحله بعد: <b>تحویل به مشتری</b>', html)

    def test_title_comes_before_the_bar_and_next_label_after_it(self):
        order = self.order(status='processing')
        self.transaction(order)
        html = self.get(order).content.decode()
        self.assertLess(html.index('od-progress-title'), html.index('role="progressbar"'))
        self.assertLess(html.index('role="progressbar"'), html.index('od-progress-next'))

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


class ShipmentProgressMappingTests(DetailBase):
    """ orders.progress.shipment_progress: وضعیت جاری، درصد و «مرحله بعد» هر وضعیت سفارش """
    def progress(self, status, paid=True, **overrides):
        from orders.progress import shipment_progress
        order = self.order(status=status, **overrides)
        if paid:
            self.transaction(order)
        return shipment_progress(Order.objects.get(pk=order.pk))

    def test_paid_pending_waits_for_processing_and_next_is_preparation(self):
        for status in ('pending', 'registered'):
            with self.subTest(status=status):
                p = self.progress(status)
                self.assertEqual((p['title'], p['next_label']), ('در انتظار پردازش', 'آماده‌سازی سفارش'))
                self.assertFalse(p['done'])

    def test_unpaid_pending_is_awaiting_payment(self):
        p = self.progress('pending', paid=False)
        self.assertEqual(p['title'], 'در انتظار پرداخت / بررسی')
        self.assertEqual(p['next_label'], 'تأیید و پردازش سفارش')

    def test_processing_next_is_handover_to_the_courier_or_post(self):
        p = self.progress('processing')
        self.assertEqual((p['title'], p['next_label'], p['percent']),
                         ('در حال آماده‌سازی در انبار', 'تحویل به مامور ارسال / پست', 50))

    def test_shipped_next_is_delivery_to_the_customer(self):
        p = self.progress('shipped')
        self.assertEqual((p['title'], p['next_label'], p['percent']), ('ارسال شده / تحویل به پست', 'تحویل به مشتری', 75))

    def test_delivered_is_done_at_100_without_a_next_step(self):
        p = self.progress('delivered')
        self.assertEqual((p['title'], p['next_label'], p['percent'], p['done']), ('تحویل مرسوله به مشتری', '', 100, True))

    def test_canceled_has_no_progress(self):
        self.assertIsNone(self.progress('canceled'))

    def test_percentages_never_decrease_along_the_lifecycle(self):
        states = [self.progress('pending', paid=False), self.progress('pending'), self.progress('processing'),
                  self.progress('shipped'), self.progress('delivered')]
        percents = [p['percent'] for p in states]
        self.assertEqual(percents, sorted(percents))
        self.assertGreater(percents[0], 0)                                               # نوار هیچ‌وقت کاملاً خالی نیست

    def test_cheque_order_without_online_payment_still_shows_its_real_stage(self):
        p = self.progress('shipped', paid=False, payment_method='check')
        self.assertEqual(p['title'], 'ارسال شده / تحویل به پست')


class ItemCardTests(DetailBase):
    def test_item_card_shows_name_color_quantity_price_and_image(self):
        color = ProductColor.objects.create(product=self.product, name='آبی نفتی', hex_code='#123456')
        order = self.make_order(self.user)
        item = self.make_order_item(order, self.product, quantity=3, price=100000)
        item.color = color
        item.save()
        html = self.get(order).content.decode()
        for text in ('کالای تست', 'آبی نفتی', '#123456', 'تعداد: <b>3</b>', '100,000', '300,000', self.product.main_image_url):
            self.assertIn(text, html)

    def test_rating_section_only_after_delivery(self):
        delivered = self.order(status='delivered')
        self.transaction(delivered)
        html = self.get(delivered).content.decode()
        self.assertEqual(html.count('data-review-star='), 5)
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
        self.assertNotIn('data-review-star=', html)                                       # کارتِ دارای نظر مودال باز نمی‌کند
        self.assertIn('data-review-trigger', html)                                        # ویرایش هم از مودال باز می‌شود
        self.assertIn('data-review=', html)

    def test_inactive_product_has_no_rating_section(self):
        order = self.order(status='delivered')
        self.transaction(order)
        self.product.is_active = False
        self.product.save()
        self.assertNotContains(self.get(order), 'od-stars')


class ReviewModalTests(DetailBase):
    """ مودال دومرحله‌ای ثبت امتیاز و دیدگاه: نشانه‌گذاری صفحه و پاسخ AJAX اندپوینت reviews:create """
    AJAX = {'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest'}

    def delivered(self):
        order = self.order(status='delivered')
        self.transaction(order)
        return order

    def create_url(self):
        return reverse('reviews:create', args=[self.product.slug])

    def test_modal_markup_and_script_are_present_for_delivered_orders(self):
        self.user.first_name = 'مریم'
        self.user.save()
        html = self.get(self.delivered()).content.decode()
        for text in ('id="reviewModal"', 'ثبت امتیاز و دیدگاه', 'مرحله ۲ از ۲', 'data-rm-back', 'data-rm-continue',
                     'data-rm-submit', 'data-rm-file', 'ارسال با نام شما: <b>مریم</b>', 'order-review-modal.js'):
            self.assertIn(text, html)
        self.assertRegex(html, r'data-rm-continue disabled')                              # بدون ستاره «ادامه» غیرفعال است
        self.assertEqual(html.count('data-rm-star '), 5)

    def test_author_label_falls_back_to_phone_like_the_site_reviews(self):
        html = self.get(self.delivered()).content.decode()
        self.assertIn(f'ارسال با نام شما: <b>{self.user.phone_number}</b>', html)

    def test_no_modal_before_delivery(self):
        shipped = self.order(status='shipped')
        self.transaction(shipped)
        html = self.get(shipped).content.decode()
        self.assertNotIn('id="reviewModal"', html)
        self.assertNotIn('order-review-modal.js', html)

    def test_card_carries_the_data_the_modal_needs(self):
        order = self.delivered()
        html = self.get(order).content.decode()
        self.assertIn(f'data-order-id="{order.pk}"', html)
        self.assertIn(f'data-product-id="{self.product.pk}"', html)
        self.assertIn('data-product-name="کالای تست"', html)
        self.assertIn(f'data-create-url="{self.create_url()}"', html)

    def test_ajax_create_returns_what_the_card_needs_to_update(self):
        self.delivered()
        response = self.client.post(self.create_url(), {'rating': '4', 'body': 'کیفیت خوب بود'}, **self.AJAX)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        review = Review.objects.get(product=self.product, user=self.user)
        self.assertTrue(data['ok'])
        self.assertEqual((data['review_id'], data['rating'], data['status']), (review.pk, 4, 'pending'))
        self.assertEqual(data['edit_url'], reverse('reviews:edit', args=[review.pk]))
        self.assertTrue(review.is_verified_purchase)

    def test_page_shows_edit_state_after_the_ajax_submit(self):
        order = self.delivered()
        self.client.post(self.create_url(), {'rating': '5', 'body': 'عالی بود'}, **self.AJAX)
        html = self.get(order).content.decode()
        self.assertEqual(html.count('od-star is-on'), 5)
        self.assertIn('ویرایش دیدگاه', html)
        self.assertNotIn('data-review-star=', html)

    def test_ajax_validation_errors_keep_the_modal_open_with_a_message(self):
        self.delivered()
        no_rating = self.client.post(self.create_url(), {'body': 'متن کافی'}, **self.AJAX)
        self.assertEqual(no_rating.status_code, 400)
        self.assertEqual(no_rating.json()['error'], 'rating_required')
        short = self.client.post(self.create_url(), {'rating': '3', 'body': 'ab'}, **self.AJAX)
        self.assertEqual(short.status_code, 400)
        self.assertEqual(short.json()['error'], 'body_too_short')
        self.assertTrue(short.json()['message'])
        self.assertFalse(Review.objects.filter(user=self.user).exists())

    def test_ajax_second_review_is_rejected_with_the_edit_redirect(self):
        self.delivered()
        self.client.post(self.create_url(), {'rating': '4', 'body': 'اولین نظر'}, **self.AJAX)
        again = self.client.post(self.create_url(), {'rating': '2', 'body': 'نظر دوم'}, **self.AJAX)
        self.assertEqual(again.status_code, 400)
        self.assertEqual(again.json()['error'], 'already_reviewed')
        self.assertEqual(Review.objects.filter(user=self.user, parent__isnull=True).count(), 1)

    def test_ajax_create_saves_at_most_three_images(self):
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==')
        files = [SimpleUploadedFile(f'p{i}.png', png, content_type='image/png') for i in range(4)]
        self.delivered()
        with override_settings(MEDIA_ROOT=media):
            response = self.client.post(self.create_url(), {'rating': '5', 'body': 'با تصویر', 'images': files}, **self.AJAX)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Review.objects.get(user=self.user, product=self.product).images.count(), 3)

    def test_script_file_keeps_the_draft_contract(self):
        from pathlib import Path
        from django.conf import settings
        js = (Path(settings.BASE_DIR) / 'static/theme/assets/js/order-review-modal.js').read_text(encoding='utf-8')
        for text in ('sessionStorage', "'orderReviewDraft:'", 'X-Requested-With', 'createUrl'):
            self.assertIn(text, js)


class MultiColorReviewTests(DetailBase):
    """
    نظر به‌ازای «کالا» ثبت می‌شود نه ردیف سفارش (قید یکتای کاربر+کالا روی Review). اگر یک کالا با دو رنگ در سفارش باشد،
    فقط اولین ردیف بخش امتیاز دارد و ردیف دوم یادداشت می‌گیرد؛ پس دو دکمه‌ی ثبت برای یک نظر نیست.
    """
    def two_color_order(self):
        order = self.make_order(self.user, status='delivered', total_price=400000)
        blue = ProductColor.objects.create(product=self.product, name='آبی', hex_code='#0000ff')
        red = ProductColor.objects.create(product=self.product, name='قرمز', hex_code='#ff0000')
        for color in (blue, red):
            item = self.make_order_item(order, self.product, quantity=1, price=200000)
            item.color = color
            item.save()
        self.transaction(order)
        return order

    def test_only_the_first_row_of_a_product_gets_the_review_section(self):
        html = self.get(self.two_color_order()).content.decode()
        self.assertEqual(html.count('data-review-item'), 1)
        self.assertEqual(html.count('data-review-trigger'), 1)
        self.assertEqual(html.count('class="od-rate-dup"'), 1)
        self.assertIn('برای همه‌ی رنگ‌ها', html)

    def test_review_state_is_shared_by_both_color_rows_without_a_second_button(self):
        order = self.two_color_order()
        Review.objects.create(product=self.product, user=self.user, rating=3, body='متوسط بود', status='published')
        html = self.get(order).content.decode()
        self.assertEqual(html.count('ویرایش دیدگاه'), 1)
        self.assertEqual(html.count('data-review-item'), 1)
        self.assertEqual(html.count('class="od-rate-dup"'), 1)

    def test_a_second_review_on_the_same_product_is_still_rejected_by_the_endpoint(self):
        order = self.two_color_order()
        url = reverse('reviews:create', args=[self.product.slug])
        ajax = {'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest'}
        self.assertEqual(self.client.post(url, {'rating': '5', 'body': 'اولی خوب بود'}, **ajax).status_code, 200)
        self.assertEqual(self.client.post(url, {'rating': '1', 'body': 'دومی بد بود'}, **ajax).status_code, 400)
        self.assertEqual(Review.objects.filter(user=self.user, product=self.product, parent__isnull=True).count(), 1)
        self.assertContains(self.get(order), 'ویرایش دیدگاه')


class EditModeTests(DetailBase):
    AJAX = {'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest'}

    def reviewed(self):
        order = self.order(status='delivered')
        self.transaction(order)
        review = Review.objects.create(product=self.product, user=self.user, rating=4, title='عنوان', body='متن اولیه', status='published')
        ReviewPoint.objects.create(review=review, kind='pro', text='کیفیت')
        ReviewPoint.objects.create(review=review, kind='con', text='قیمت')
        return order, review

    def test_card_embeds_the_existing_review_for_the_modal_edit_mode(self):
        order, review = self.reviewed()
        html = self.get(order).content.decode()
        m = re.search(r'data-review="([^"]*)"', html)
        self.assertIsNotNone(m)
        data = json.loads(unescape(m.group(1)))
        self.assertEqual((data['review_id'], data['rating'], data['title'], data['body']), (review.pk, 4, 'عنوان', 'متن اولیه'))
        self.assertEqual((data['pros'], data['cons']), (['کیفیت'], ['قیمت']))
        self.assertEqual(data['edit_url'], reverse('reviews:edit', args=[review.pk]))

    def test_ajax_edit_updates_in_place_and_returns_the_new_state(self):
        order, review = self.reviewed()
        response = self.client.post(reverse('reviews:edit', args=[review.pk]), {
            'rating': '2', 'body': 'متن ویرایش‌شده', 'title': 'عنوان', 'pros': ['سبک'], 'cons': ['صدا', 'قیمت'],
        }, **self.AJAX)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['ok'])
        self.assertEqual((data['rating'], data['body'], data['status']), (2, 'متن ویرایش‌شده', 'pending'))
        self.assertEqual((data['pros'], data['cons']), (['سبک'], ['صدا', 'قیمت']))
        review.refresh_from_db()
        self.assertEqual((review.rating, review.status), (2, 'pending'))                  # ویرایش دوباره در صف تأیید می‌رود

    def test_create_ajax_accepts_pros_and_cons_and_returns_them(self):
        order = self.order(status='delivered')
        self.transaction(order)
        response = self.client.post(reverse('reviews:create', args=[self.product.slug]), {
            'rating': '5', 'body': 'عالی بود', 'pros': ['ماندگار', ' '], 'cons': ['گران'],
        }, **self.AJAX)
        data = response.json()
        self.assertEqual((data['pros'], data['cons']), (['ماندگار'], ['گران']))             # مورد خالی نادیده گرفته می‌شود
        review = Review.objects.get(user=self.user, product=self.product)
        self.assertEqual(review.points.filter(kind='pro').count(), 1)

    def test_modal_markup_has_optional_points_fields_and_edit_labels(self):
        order, _review = self.reviewed()
        html = self.get(order).content.decode()
        for text in ('data-rm-points="pros"', 'data-rm-points="cons"', 'نقاط قوت (اختیاری)', 'نقاط ضعف (اختیاری)',
                     'data-rm-edit-notice', 'data-rm-title'):
            self.assertIn(text, html)

    def test_script_supports_edit_mode_and_points(self):
        from pathlib import Path
        from django.conf import settings
        js = (Path(settings.BASE_DIR) / 'static/theme/assets/js/order-review-modal.js').read_text(encoding='utf-8')
        for text in ("'ثبت تغییرات'", 'remove_image', "mode: review ? 'edit' : 'create'", 'edit_url', "data.append('pros'"):
            self.assertIn(text, js)


class SidebarAndMoneyTests(DetailBase):
    def test_sidebar_has_no_global_review_button(self):
        order = self.order(status='delivered')
        self.transaction(order)
        html = self.get(order).content.decode()
        self.assertNotIn('ثبت نظر درباره محصولات', html)
        self.assertNotIn(reverse('orders:order_reviews', args=[order.pk]), html)

    def test_amounts_use_a_three_digit_separator_everywhere_on_the_page(self):
        order = self.make_order(self.user, status='delivered', total_price=1164760, shipping_cost=45000,
                                promotion_discount=50000, order_discount=15000)
        self.make_order_item(order, self.product, quantity=2, price=600000)
        self.transaction(order, amount=Decimal('964760'), wallet_amount=Decimal('200000'))
        html = self.get(order).content.decode()
        for text in ('1,164,760', '1,200,000', '45,000', '50,000', '15,000', '200,000'):
            self.assertIn(text, html)
        # هیچ مبلغ ۵ رقمی یا بیشتر بدون جداکننده نمانده (مثل 1164760 تومان)
        self.assertNotRegex(html, r'(?<![\d,])\d{5,}(\s|<[^>]+>)*تومان')

    def test_money_filter(self):
        from orders.templatetags.money import money
        self.assertEqual(money(0), '0')
        self.assertEqual(money(999), '999')
        self.assertEqual(money(1000), '1,000')
        self.assertEqual(money(Decimal('1164760')), '1,164,760')
        self.assertEqual(money(Decimal('-50000')), '-50,000')
        self.assertEqual(money('2500000.4'), '2,500,000')
        self.assertEqual(money(None), '')
        self.assertEqual(money('abc'), '')


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
