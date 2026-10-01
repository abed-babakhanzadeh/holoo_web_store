"""
تست‌های تجربه‌ی کاربری ویزارد مرجوعی: حفظ انتخاب‌ها هنگام برگشت به گام‌های قبل، نگه‌داشتن/حذف مدارک قبلاً
آپلودشده، پاک‌سازی فایل‌های موقتِ اقلام حذف‌شده، تصویر کوچک ردیف‌ها و نمایش وضعیت درخواست‌ها در جزئیات سفارش.
"""
import tempfile

from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from returns.models import ReturnRequest
from returns.tests_views import ReturnWizardTestBase
from returns.views import SESSION_KEY

TINY_GIF = (b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,'
            b'\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;')


class WizardUxBase(ReturnWizardTestBase):
    def setUp(self):
        super().setUp()
        self._media = tempfile.TemporaryDirectory()
        self.addCleanup(self._media.cleanup)
        override = override_settings(MEDIA_ROOT=self._media.name)
        override.enable()
        self.addCleanup(override.disable)

    def png(self, name='proof.png'):
        return SimpleUploadedFile(name, TINY_GIF, content_type='image/png')

    def go_step2(self, order, item, qty=1):
        return self.client.post(self.urls(order.id)['step1'], {f'quantity_{item.pk}': str(qty)})

    def post_step2(self, order, item, **extra):
        data = {f'reason_{item.pk}': str(self.reason.pk), f'description_{item.pk}': 'توضیح من'}
        data.update(extra)
        return self.client.post(self.urls(order.id)['step2'], data)


class StepOneStateTests(WizardUxBase):
    def test_fresh_wizard_starts_at_zero(self):
        order, item = self.make_deliverable_order(quantity=3)
        response = self.client.get(self.urls(order.id)['step1'])
        self.assertContains(response, f'name="quantity_{item.pk}"')
        self.assertContains(response, 'value="0"')

    def test_selection_survives_going_back_from_step_two(self):
        order, item = self.make_deliverable_order(quantity=5)
        self.go_step2(order, item, qty=3)
        response = self.client.get(self.urls(order.id)['step1'])
        self.assertContains(response, 'value="3"')
        self.assertContains(response, 'max="5"')

    def test_saved_quantity_is_clamped_to_current_capacity(self):
        order, item = self.make_deliverable_order(quantity=5)
        self.go_step2(order, item, qty=5)
        session = self.client.session
        session[SESSION_KEY]['step1'][str(item.pk)] = 99                      # ظرفیت از آن موقع کمتر شده
        session.save()
        response = self.client.get(self.urls(order.id)['step1'])
        self.assertContains(response, 'value="5"')
        self.assertNotContains(response, 'value="99"')

    def test_other_orders_session_does_not_prefill(self):
        order_a, item_a = self.make_deliverable_order(quantity=2)
        order_b, item_b = self.make_deliverable_order(quantity=2)
        self.go_step2(order_a, item_a, qty=2)
        response = self.client.get(self.urls(order_b.id)['step1'])
        self.assertNotContains(response, 'value="2"')

    def test_item_images_use_the_small_thumb_class_everywhere(self):
        order, item = self.make_deliverable_order(quantity=2)
        step1 = self.client.get(self.urls(order.id)['step1']).content.decode()
        self.assertIn('class="rt-thumb"', step1)
        self.assertNotIn('w-14 h-14', step1)                                   # این کلاس‌ها در app.css کامپایل نشده‌اند
        self.go_step2(order, item)
        step2 = self.client.get(self.urls(order.id)['step2']).content.decode()
        self.assertIn('class="rt-thumb"', step2)
        self.assertNotIn('w-14 h-14', step2)
        self.post_step2(order, item)
        step3 = self.client.get(self.urls(order.id)['step3']).content.decode()
        self.assertIn('class="rt-thumb"', step3)


class StepTwoStateTests(WizardUxBase):
    def test_reason_and_description_are_prefilled_when_coming_back_from_step_three(self):
        order, item = self.make_deliverable_order(quantity=2)
        self.go_step2(order, item)
        self.post_step2(order, item)
        response = self.client.get(self.urls(order.id)['step2'])
        self.assertContains(response, f'<option value="{self.reason.pk}" selected>')
        self.assertContains(response, 'توضیح من')

    def test_uploaded_proofs_are_kept_when_step_two_is_posted_again_without_files(self):
        order, item = self.make_deliverable_order(quantity=2)
        self.go_step2(order, item)
        self.post_step2(order, item, **{f'attachments_{item.pk}': self.png()})
        staged = self.client.session[SESSION_KEY]['step2'][str(item.pk)]['attachments']
        self.assertEqual(len(staged), 1)
        temp_path = staged[0]['temp_path']
        self.assertTrue(default_storage.exists(temp_path))

        page = self.client.get(self.urls(order.id)['step2'])
        self.assertContains(page, 'proof.png')
        self.assertContains(page, f'name="remove_attachments_{item.pk}"')

        self.post_step2(order, item)                                           # بدون فایل جدید
        staged_after = self.client.session[SESSION_KEY]['step2'][str(item.pk)]['attachments']
        self.assertEqual([a['temp_path'] for a in staged_after], [temp_path])
        self.assertTrue(default_storage.exists(temp_path))

    def test_ticking_remove_deletes_the_staged_file(self):
        order, item = self.make_deliverable_order(quantity=2)
        self.go_step2(order, item)
        self.post_step2(order, item, **{f'attachments_{item.pk}': self.png()})
        temp_path = self.client.session[SESSION_KEY]['step2'][str(item.pk)]['attachments'][0]['temp_path']
        self.post_step2(order, item, **{f'remove_attachments_{item.pk}': ['0']})
        self.assertEqual(self.client.session[SESSION_KEY]['step2'][str(item.pk)]['attachments'], [])
        self.assertFalse(default_storage.exists(temp_path))

    def test_cap_counts_kept_plus_new_files(self):
        order, item = self.make_deliverable_order(quantity=2)
        self.go_step2(order, item)
        self.post_step2(order, item, **{f'attachments_{item.pk}': [self.png(f'a{i}.png') for i in range(4)]})
        response = self.post_step2(order, item, **{f'attachments_{item.pk}': [self.png('x1.png'), self.png('x2.png')]})
        self.assertEqual(response.status_code, 200)                           # 4 قبلی + 2 جدید > 5
        self.assertContains(response, 'حداکثر 5 فایل')
        ok = self.post_step2(order, item, **{f'attachments_{item.pk}': [self.png('y1.png')]})
        self.assertEqual(ok.status_code, 302)                                  # 4 + 1 = 5 مجاز است
        self.assertEqual(len(self.client.session[SESSION_KEY]['step2'][str(item.pk)]['attachments']), 5)


class StepOneRepostTests(WizardUxBase):
    def setUp(self):
        super().setUp()
        self.order, self.item_a = self.make_deliverable_order(quantity=3)
        self.product_b = self.make_product(self.category, name='کالای دوم', slug='second-product', erp_code='ERP-RETURNS-2')
        self.item_b = self.make_order_item(self.order, self.product_b, price=40000, quantity=2)

    def _walk_to_step3_with_both(self):
        self.client.post(self.urls(self.order.id)['step1'],
                         {f'quantity_{self.item_a.pk}': '1', f'quantity_{self.item_b.pk}': '1'})
        self.client.post(self.urls(self.order.id)['step2'], {
            f'reason_{self.item_a.pk}': str(self.reason.pk), f'description_{self.item_a.pk}': 'الف',
            f'reason_{self.item_b.pk}': str(self.reason.pk), f'description_{self.item_b.pk}': 'ب',
            f'attachments_{self.item_b.pk}': self.png('b.png'),
        })

    def test_reposting_step_one_keeps_details_of_items_that_stay_selected(self):
        self._walk_to_step3_with_both()
        self.client.post(self.urls(self.order.id)['step1'], {f'quantity_{self.item_a.pk}': '2', f'quantity_{self.item_b.pk}': '1'})
        data = self.client.session[SESSION_KEY]
        self.assertEqual(data['step1'], {str(self.item_a.pk): 2, str(self.item_b.pk): 1})
        self.assertEqual(data['step2'][str(self.item_a.pk)]['description'], 'الف')
        self.assertEqual(len(data['step2'][str(self.item_b.pk)]['attachments']), 1)
        self.assertEqual(self.client.get(self.urls(self.order.id)['step3']).status_code, 200)

    def test_deselected_item_loses_its_details_and_temp_files(self):
        self._walk_to_step3_with_both()
        temp_path = self.client.session[SESSION_KEY]['step2'][str(self.item_b.pk)]['attachments'][0]['temp_path']
        self.assertTrue(default_storage.exists(temp_path))
        self.client.post(self.urls(self.order.id)['step1'], {f'quantity_{self.item_a.pk}': '1', f'quantity_{self.item_b.pk}': '0'})
        data = self.client.session[SESSION_KEY]
        self.assertEqual(list(data['step1']), [str(self.item_a.pk)])
        self.assertNotIn(str(self.item_b.pk), data['step2'])
        self.assertFalse(default_storage.exists(temp_path))

    def test_newly_selected_item_without_details_sends_step_three_back_instead_of_crashing(self):
        self.client.post(self.urls(self.order.id)['step1'], {f'quantity_{self.item_a.pk}': '1'})
        self.client.post(self.urls(self.order.id)['step2'], {
            f'reason_{self.item_a.pk}': str(self.reason.pk), f'description_{self.item_a.pk}': 'الف',
        })
        self.client.post(self.urls(self.order.id)['step1'], {f'quantity_{self.item_a.pk}': '1', f'quantity_{self.item_b.pk}': '1'})
        response = self.client.get(self.urls(self.order.id)['step3'])
        self.assertRedirects(response, self.urls(self.order.id)['step1'])
        response = self.client.post(self.urls(self.order.id)['step3'], {'refund_method': 'wallet'})
        self.assertRedirects(response, self.urls(self.order.id)['step1'], fetch_redirect_response=False)
        self.assertEqual(ReturnRequest.objects.count(), 0)

    def test_starting_another_order_deletes_the_abandoned_wizards_temp_files(self):
        self._walk_to_step3_with_both()
        temp_path = self.client.session[SESSION_KEY]['step2'][str(self.item_b.pk)]['attachments'][0]['temp_path']
        other_order, other_item = self.make_deliverable_order(quantity=2)
        self.client.post(self.urls(other_order.id)['step1'], {f'quantity_{other_item.pk}': '1'})
        self.assertFalse(default_storage.exists(temp_path))
        self.assertEqual(self.client.session[SESSION_KEY]['order_id'], other_order.id)
        self.assertNotIn('step2', self.client.session[SESSION_KEY])


class OrderDetailReturnListTests(WizardUxBase):
    def test_submitted_return_requests_are_listed_on_the_order_page(self):
        order, _item = self.make_deliverable_order(quantity=2)
        request_obj = self.make_return_request(order, self.customer)
        url = reverse('orders:order_detail_full', args=[order.id])
        response = self.client.get(url)
        self.assertContains(response, 'درخواست‌های مرجوعی این سفارش')
        self.assertContains(response, f'#{request_obj.pk}')
        self.assertContains(response, request_obj.get_status_display())

    def test_section_is_hidden_without_return_requests(self):
        order, _item = self.make_deliverable_order(quantity=2)
        response = self.client.get(reverse('orders:order_detail_full', args=[order.id]))
        self.assertNotContains(response, 'درخواست‌های مرجوعی این سفارش')
