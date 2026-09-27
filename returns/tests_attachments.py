"""
تست‌های Part C.3: اعتبارسنجی مدارک در ReturnStepTwoForm، و ذخیره‌سازی/اتصال واقعی فایل‌ها به
ReturnAttachment از طریق کل ویزارد (create_return_request).
"""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.files.storage import default_storage
from django.test import TestCase
from django.urls import reverse
from django.utils.datastructures import MultiValueDict

from products.models import SiteSettings
from returns.forms import ReturnStepTwoForm
from returns.models import ReturnAttachment
from returns.tests import ReturnsTestMixin


def make_image(name='photo.jpg', size=1024, content_type='image/jpeg'):
    return SimpleUploadedFile(name, b'0' * size, content_type=content_type)


def make_video(name='clip.mp4', size=2048, content_type='video/mp4'):
    return SimpleUploadedFile(name, b'0' * size, content_type=content_type)


class ReturnStepTwoFormAttachmentTests(ReturnsTestMixin, TestCase):
    def setUp(self):
        self.user = self.make_user()
        self.category = self.make_category()
        self.product = self.make_product(self.category)
        self.order = self.make_order(self.user)
        self.order_item = self.make_order_item(self.order, self.product, quantity=5)
        self.reason = self.make_reason()

    def _form(self, uploads):
        field_name = f'attachments_{self.order_item.pk}'
        data = {
            f'reason_{self.order_item.pk}': str(self.reason.pk),
            f'description_{self.order_item.pk}': 'توضیح تست',
        }
        files = MultiValueDict({field_name: uploads}) if uploads else MultiValueDict()
        return ReturnStepTwoForm(data, files, order_items=[self.order_item])

    def test_valid_mix_of_image_and_video_up_to_five_is_accepted(self):
        uploads = [make_image('a.jpg'), make_image('b.png'), make_video('c.mp4'), make_video('d.mov'), make_image('e.webp')]
        form = self._form(uploads)
        self.assertTrue(form.is_valid(), form.errors)
        attachments = form.attachments_by_item()[self.order_item.pk]
        self.assertEqual(len(attachments), 5)
        types = {a['attachment_type'] for a in attachments}
        self.assertEqual(types, {ReturnAttachment.IMAGE, ReturnAttachment.VIDEO})

    def test_sixth_file_is_rejected(self):
        uploads = [make_image(f'{i}.jpg') for i in range(6)]
        form = self._form(uploads)
        self.assertFalse(form.is_valid())
        self.assertIn(f'attachments_{self.order_item.pk}', form.errors)
        self.assertIn('حداکثر', str(form.errors[f'attachments_{self.order_item.pk}']))

    def test_disallowed_extension_is_rejected(self):
        bad = SimpleUploadedFile('malware.exe', b'x' * 100, content_type='application/octet-stream')
        form = self._form([make_image('ok.jpg'), bad])
        self.assertFalse(form.is_valid())
        self.assertIn(f'attachments_{self.order_item.pk}', form.errors)
        self.assertIn('فرمت', str(form.errors[f'attachments_{self.order_item.pk}']))

    def test_gif_extension_is_rejected(self):
        """ gif عمداً در فهرست مجاز نیست (فقط jpg/jpeg/png/webp) """
        bad = SimpleUploadedFile('anim.gif', b'x' * 100, content_type='image/gif')
        form = self._form([bad])
        self.assertFalse(form.is_valid())

    def test_oversized_image_is_rejected_using_site_settings_limit(self):
        settings_obj = SiteSettings.load()
        settings_obj.return_attachment_max_image_mb = 1
        settings_obj.save()
        self.addCleanup(self._reset_settings)

        oversized = make_image('big.jpg', size=(2 * 1024 * 1024))
        form = self._form([oversized])
        self.assertFalse(form.is_valid())
        self.assertIn('حجم', str(form.errors[f'attachments_{self.order_item.pk}']))

    def test_no_files_is_valid_attachments_are_optional(self):
        form = self._form([])
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.attachments_by_item()[self.order_item.pk], [])

    @staticmethod
    def _reset_settings():
        settings_obj = SiteSettings.load()
        settings_obj.return_attachment_max_image_mb = 5
        settings_obj.save()


class WizardAttachmentEndToEndTests(ReturnsTestMixin, TestCase):
    """ کل مسیر واقعی: آپلود در گام ۲ (ذخیره‌ی موقت) -> ثبت نهایی در گام ۳ (ReturnAttachment واقعی) """

    def setUp(self):
        self.user = self.make_user()
        self.category = self.make_category()
        self.product = self.make_product(self.category)
        self.order = self.make_order(self.user)
        self.order_item = self.make_order_item(self.order, self.product, quantity=2)
        self.reason = self.make_reason()
        self.client.force_login(self.user)
        self.urls = {
            'step1': reverse('returns:wizard_step1', args=[self.order.id]),
            'step2': reverse('returns:wizard_step2', args=[self.order.id]),
            'step3': reverse('returns:wizard_step3', args=[self.order.id]),
        }
        self.addCleanup(self._cleanup_media)

    def _cleanup_media(self):
        import shutil
        from django.conf import settings as dj_settings
        target = dj_settings.MEDIA_ROOT
        for sub in ('returns/attachments', 'returns/tmp_uploads'):
            path = f'{target}/{sub}'
            shutil.rmtree(path, ignore_errors=True)

    def _walk_to_step_three_with_attachments(self, uploads):
        self.client.post(self.urls['step1'], {f'quantity_{self.order_item.pk}': '2'})
        field_name = f'attachments_{self.order_item.pk}'
        data = {
            f'reason_{self.order_item.pk}': str(self.reason.pk),
            f'description_{self.order_item.pk}': 'توضیح تست یکپارچه',
        }
        data[field_name] = uploads
        return self.client.post(self.urls['step2'], data)

    def test_valid_uploads_are_staged_as_temp_files_after_step_two(self):
        response = self._walk_to_step_three_with_attachments([make_image('a.jpg'), make_video('b.mp4')])
        self.assertRedirects(response, self.urls['step3'])

        session_data = self.client.session['return_wizard_data']
        staged = session_data['step2'][str(self.order_item.pk)]['attachments']
        self.assertEqual(len(staged), 2)
        for attachment in staged:
            self.assertTrue(default_storage.exists(attachment['temp_path']))

    def test_sixth_file_blocks_step_two_submission(self):
        uploads = [make_image(f'{i}.jpg') for i in range(6)]
        response = self._walk_to_step_three_with_attachments(uploads)
        self.assertEqual(response.status_code, 200)   # فرم دوباره با خطا نمایش داده می‌شود
        self.assertNotIn('step2', self.client.session.get('return_wizard_data', {}))

    def test_disallowed_extension_blocks_step_two_submission(self):
        bad = SimpleUploadedFile('script.exe', b'x' * 10, content_type='application/octet-stream')
        response = self._walk_to_step_three_with_attachments([bad])
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('step2', self.client.session.get('return_wizard_data', {}))

    def test_full_submission_creates_return_attachment_rows_saved_on_disk(self):
        self._walk_to_step_three_with_attachments([make_image('proof.jpg'), make_video('proof.mp4')])
        response = self.client.post(self.urls['step3'], {'refund_method': 'wallet'})
        self.assertEqual(response.status_code, 302)

        from returns.models import ReturnItem, ReturnRequest
        return_request = ReturnRequest.objects.get(order=self.order)
        return_item = ReturnItem.objects.get(return_request=return_request)
        attachments = list(ReturnAttachment.objects.filter(return_item=return_item))

        self.assertEqual(len(attachments), 2)
        types = {a.attachment_type for a in attachments}
        self.assertEqual(types, {ReturnAttachment.IMAGE, ReturnAttachment.VIDEO})
        for attachment in attachments:
            self.assertTrue(attachment.file.storage.exists(attachment.file.name))
            self.assertTrue(attachment.original_filename)

    def test_temp_files_are_deleted_after_successful_final_submission(self):
        self._walk_to_step_three_with_attachments([make_image('temp-cleanup.jpg')])
        session_data = self.client.session['return_wizard_data']
        temp_path = session_data['step2'][str(self.order_item.pk)]['attachments'][0]['temp_path']
        self.assertTrue(default_storage.exists(temp_path))

        self.client.post(self.urls['step3'], {'refund_method': 'wallet'})
        self.assertFalse(default_storage.exists(temp_path))

    def test_attachments_are_isolated_per_item_not_per_request(self):
        """ الزام خط قرمز: مدارک منحصراً به هر قلم وصل‌اند، نه به کل درخواست """
        second_product = self.make_product(self.category, name='محصول دوم', slug='attach-test-second', erp_code='ERP-ATTACH-2')
        second_item = self.make_order_item(self.order, second_product, quantity=1)

        self.client.post(self.urls['step1'], {
            f'quantity_{self.order_item.pk}': '1', f'quantity_{second_item.pk}': '1',
        })
        self.client.post(self.urls['step2'], {
            f'reason_{self.order_item.pk}': str(self.reason.pk), f'description_{self.order_item.pk}': 'اول',
            f'attachments_{self.order_item.pk}': [make_image('first-item.jpg')],
            f'reason_{second_item.pk}': str(self.reason.pk), f'description_{second_item.pk}': 'دوم',
        })
        self.client.post(self.urls['step3'], {'refund_method': 'wallet'})

        from returns.models import ReturnItem
        first_return_item = ReturnItem.objects.get(order_item=self.order_item)
        second_return_item = ReturnItem.objects.get(order_item=second_item)
        self.assertEqual(ReturnAttachment.objects.filter(return_item=first_return_item).count(), 1)
        self.assertEqual(ReturnAttachment.objects.filter(return_item=second_return_item).count(), 0)
