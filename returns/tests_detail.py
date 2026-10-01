"""
صفحه‌ی جزئیات درخواست مرجوعی (returns.views.ReturnDetailView + templates/returns/return_detail.html):
دسترسی مالک، تایم‌لاین، کادر آدرس ارسال، کارت اقلام، سفارش مرجع و لینک‌های ورودی.
"""
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from orders.models import Order
from products.models import ProductColor, SiteSettings
from returns.models import ReturnItem, ReturnRequest
from returns.tests import ReturnsTestMixin
from returns.timeline import (
    STATE_CURRENT, STATE_DONE, STATE_FAILED, STATE_PENDING, STATE_SKIPPED, build_timeline, shipping_summary,
)

S = ReturnRequest


class DetailBase(ReturnsTestMixin, TestCase):
    def setUp(self):
        self.user = self.make_user()
        self.category = self.make_category()
        self.product = self.make_product(self.category)
        self.reason = self.make_reason(title='مغایرت با تصویر')
        self.order = self.make_order(self.user, shipping_cost=45000, total_price=245000)
        self.item = self.make_order_item(self.order, self.product, quantity=2, price=100000)
        self.client.force_login(self.user)
        self.addCleanup(cache.delete, SiteSettings.CACHE_KEY)

    def make_return(self, status=S.STATUS_PENDING, **fields):
        now = timezone.now()
        defaults = {}
        if status != S.STATUS_PENDING:
            defaults['decided_at'] = now - timedelta(days=3)
        if status in (S.STATUS_ITEM_RECEIVED, S.STATUS_REFUND_PENDING, S.STATUS_COMPLETED):
            defaults['item_received_at'] = now - timedelta(days=2)
        if status == S.STATUS_COMPLETED:
            defaults['completed_at'] = now - timedelta(days=1)
        if status == S.STATUS_REJECTED:
            defaults['rejection_reason'] = 'کالا استفاده شده است'
        defaults.update(fields)
        request = self.make_return_request(self.order, self.user, status=status, **defaults)
        item_fields = {}
        if status in (S.STATUS_REFUND_PENDING, S.STATUS_COMPLETED):
            item_fields = {'approved_quantity': 1, 'refund_amount': Decimal('100000')}
        ReturnItem.objects.create(return_request=request, order_item=self.item, reason=self.reason,
                                  requested_quantity=1, **item_fields)
        return request

    def url(self, request):
        return reverse('returns:detail', args=[request.pk])

    def html(self, request):
        return self.client.get(self.url(request)).content.decode()

    def set_store(self, **fields):
        settings_obj = SiteSettings.load()
        for key, value in fields.items():
            setattr(settings_obj, key, value)
        settings_obj.save()
        cache.delete(SiteSettings.CACHE_KEY)


class AccessTests(DetailBase):
    def test_owner_sees_the_page(self):
        response = self.client.get(self.url(self.make_return()))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'returns/return_detail.html')
        self.assertContains(response, 'جزئیات درخواست مرجوعی')

    def test_other_users_request_is_404_and_anonymous_is_redirected(self):
        request = self.make_return()
        other = self.make_user('09140009998')
        self.client.force_login(other)
        self.assertEqual(self.client.get(self.url(request)).status_code, 404)
        self.client.logout()
        response = self.client.get(self.url(request))
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response.url)

    def test_unknown_request_is_404(self):
        self.assertEqual(self.client.get(reverse('returns:detail', args=[999999])).status_code, 404)


class TimelineStateTests(DetailBase):
    def states(self, status, **fields):
        return [step['state'] for step in build_timeline(self.make_return(status, **fields))]

    def test_pending(self):
        self.assertEqual(self.states(S.STATUS_PENDING), [STATE_DONE, STATE_CURRENT, STATE_PENDING, STATE_PENDING])

    def test_approved_waits_for_the_customer_to_ship(self):
        self.assertEqual(self.states(S.STATUS_APPROVED), [STATE_DONE, STATE_DONE, STATE_CURRENT, STATE_PENDING])

    def test_item_received_is_under_final_inspection(self):
        steps = build_timeline(self.make_return(S.STATUS_ITEM_RECEIVED))
        self.assertEqual([s['state'] for s in steps], [STATE_DONE, STATE_DONE, STATE_DONE, STATE_CURRENT])
        self.assertEqual(steps[2]['badge'], 'کالاها دریافت شدند')
        self.assertEqual(steps[3]['note'], 'در حال بازرسی نهایی کالا')

    def test_refund_pending_is_the_current_refund_step(self):
        steps = build_timeline(self.make_return(S.STATUS_REFUND_PENDING))
        self.assertEqual(steps[3]['state'], STATE_CURRENT)
        self.assertEqual(steps[3]['note'], 'در صف بازپرداخت')

    def test_completed_is_all_done_with_dates(self):
        steps = build_timeline(self.make_return(S.STATUS_COMPLETED))
        self.assertEqual({s['state'] for s in steps}, {STATE_DONE})
        self.assertTrue(all(s['date'] for s in steps))

    def test_rejected_before_receipt_fails_step_two_and_skips_the_rest(self):
        steps = build_timeline(self.make_return(S.STATUS_REJECTED))
        self.assertEqual([s['state'] for s in steps], [STATE_DONE, STATE_FAILED, STATE_SKIPPED, STATE_SKIPPED])
        self.assertEqual(steps[1]['note'], 'کالا استفاده شده است')

    def test_rejected_after_receipt_keeps_the_received_step_done(self):
        steps = build_timeline(self.make_return(S.STATUS_REJECTED, item_received_at=timezone.now()))
        self.assertEqual([s['state'] for s in steps], [STATE_DONE, STATE_FAILED, STATE_DONE, STATE_SKIPPED])


class PageContentTests(DetailBase):
    def test_timeline_renders_jalali_dates_and_the_green_received_badge(self):
        html = self.html(self.make_return(S.STATUS_ITEM_RECEIVED))
        for text in ('ثبت درخواست', 'نتیجه‌ی بررسی درخواست', 'دریافت کالا توسط فروشگاه', 'بازپرداخت و تکمیل نهایی',
                     'کالاها دریافت شدند', 'rd-step is-done', 'rd-step is-current'):
            self.assertIn(text, html)
        self.assertRegex(html, r'14\d\d/\d\d/\d\d - \d\d:\d\d')

    def test_rejected_page_shows_the_reason_banner(self):
        html = self.html(self.make_return(S.STATUS_REJECTED))
        self.assertIn('درخواست مرجوعی شما رد شد', html)
        self.assertIn('دلیل: کالا استفاده شده است', html)

    def test_header_has_back_link_and_invoice_placeholder(self):
        html = self.html(self.make_return())
        self.assertIn(f'{reverse("orders:order_history")}?tab=returned', html)
        self.assertRegex(html, r'<button type="button" class="od-btn is-sm" disabled title="[^"]+">\s*<svg[^>]*>.*?</svg>\s*فاکتور')

    def test_item_card_shows_product_color_reason_quantity(self):
        color = ProductColor.objects.create(product=self.product, name='آبی نفتی', hex_code='#123456')
        self.item.color = color
        self.item.save()
        html = self.html(self.make_return())
        for text in ('کالای تست', 'آبی نفتی', '#123456', 'علت مرجوعی', 'مغایرت با تصویر', 'تعداد درخواستی: <b>1</b>',
                     self.product.main_image_url):
            self.assertIn(text, html)

    def test_refund_amounts_are_hidden_until_inspection_is_finalised(self):
        for status in (S.STATUS_PENDING, S.STATUS_APPROVED, S.STATUS_ITEM_RECEIVED):
            with self.subTest(status=status):
                S.objects.all().delete()
                html = self.html(self.make_return(status))
                self.assertIn('مبلغ استرداد پس از بررسی و بازرسی کالا مشخص می‌شود', html)
                self.assertIn('پس از بازرسی کالا مشخص می‌شود', html)

    def test_final_amounts_use_the_thousand_separator(self):
        request = self.make_return(S.STATUS_COMPLETED, shipping_refunded=True, shipping_refund_amount=Decimal('45000'))
        html = self.html(request)
        self.assertIn('<b>100,000</b>', html)                                             # مبلغ استرداد قلم
        self.assertIn('<b>145,000</b>', html)                                             # جمع کل = قلم + کرایه
        self.assertIn('<b>45,000</b>', html)                                              # بازگشت هزینه‌ی ارسال سفارش
        self.assertNotRegex(html, r'(?<![\d,])\d{5,}(\s|<[^>]+>)*تومان')

    def test_order_box_links_back_to_the_order(self):
        request = self.make_return()
        html = self.html(request)
        self.assertIn(reverse('orders:order_detail_full', args=[self.order.pk]), html)
        self.assertIn(f'#{self.order.pk}', html)
        self.assertRegex(html, r'14\d\d/\d\d/\d\d - \d\d:\d\d')
        self.assertIn('روش بازپرداخت', html)


class ShippingBoxTests(DetailBase):
    def test_box_with_store_address_and_guidance_before_receipt(self):
        self.set_store(store_name='بازرگانی موسوی', store_address='قم، بلوار امین، پلاک ۱۲', store_postal_code='3714912345',
                       store_phone_1='02537700000')
        for status in (S.STATUS_PENDING, S.STATUS_APPROVED):
            with self.subTest(status=status):
                S.objects.all().delete()
                request = self.make_return(status)
                html = self.html(request)
                for text in ('ارسال مرجوعی به فروشگاه', 'قم، بلوار امین، پلاک ۱۲', '3714912345', '02537700000',
                             'جعبه‌ی اصلی کالا را نگه دارید', f'کد مرجوعی <b>#{request.pk}</b>'):
                    self.assertIn(text, html)
        self.assertIn('درخواست شما تأیید شد', self.html(S.objects.get()))
        S.objects.all().delete()
        self.assertIn('پس از تأیید درخواست توسط فروشگاه', self.html(self.make_return(S.STATUS_PENDING)))

    def test_box_is_hidden_once_the_store_has_the_item_or_the_request_is_closed(self):
        self.set_store(store_address='قم')
        for status in (S.STATUS_ITEM_RECEIVED, S.STATUS_REFUND_PENDING, S.STATUS_COMPLETED, S.STATUS_REJECTED):
            with self.subTest(status=status):
                S.objects.all().delete()
                self.assertNotIn('ارسال مرجوعی به فروشگاه', self.html(self.make_return(status)))

    def test_missing_store_address_shows_a_fallback_instead_of_an_empty_box(self):
        self.set_store(store_address='')
        self.assertIn('آدرس فروشگاه هنوز ثبت نشده است', self.html(self.make_return(S.STATUS_APPROVED)))

    def test_shipping_summary_by_reason_payer(self):
        store_reason = self.make_reason(title='کالای معیوب', shipping_cost_payer='store')
        request = self.make_return()
        items = list(request.items.select_related('reason'))
        self.assertFalse(shipping_summary(request, items)['free'])
        ReturnItem.objects.filter(pk=items[0].pk).update(reason=store_reason)
        items = list(request.items.select_related('reason'))
        self.assertTrue(shipping_summary(request, items)['free'])
        self.assertIn('رایگان', self.html(request))
        self.assertIsNone(shipping_summary(request, [])['free'])


class QueryAndLinksTests(DetailBase):
    def test_query_count_does_not_grow_with_the_number_of_items(self):
        small = self.make_return()
        order = self.make_order(self.user)
        big = self.make_return_request(order, self.user)
        for index in range(6):
            product = self.make_product(self.category, name=f'کالا {index}', slug=f'rd-{index}', erp_code=f'ERP-RD-{index}')
            ReturnItem.objects.create(return_request=big, order_item=self.make_order_item(order, product), reason=self.reason,
                                      requested_quantity=1)
        self.client.get(self.url(small))                                                  # گرم‌کردن کش تنظیمات سایت
        with CaptureQueriesContext(connection) as small_queries:
            self.client.get(self.url(small))
        with self.assertNumQueries(len(small_queries)):
            self.client.get(self.url(big))

    def test_returned_tab_cards_link_to_the_detail_page(self):
        request = self.make_return()
        html = self.client.get(reverse('orders:order_history'), {'tab': 'returned'}).content.decode()
        self.assertIn(f'href="{self.url(request)}" class="oh-card-head"', html)

    def test_success_page_and_order_detail_link_to_the_detail_page(self):
        request = self.make_return()
        success = self.client.get(reverse('returns:wizard_success', args=[request.pk])).content.decode()
        self.assertIn(self.url(request), success)
        order_page = self.client.get(reverse('orders:order_detail_full', args=[self.order.pk])).content.decode()
        self.assertIn(self.url(request), order_page)
