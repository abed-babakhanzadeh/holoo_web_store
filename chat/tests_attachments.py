"""
فاز ۴ - پیوست‌ها: تشخیص نوع از magic bytes (نه پسوند)، پاک‌سازی EXIF/داده‌ی چسبیده با Pillow، سخت‌سازی PDF، سقف حجم/تعداد،
ذخیره‌ی امن (uuid، بدون نشانی عمومی، فقط زیر MEDIA_ROOT موقتِ تست)، دانلود دارای کنترل دسترسی (IDOR، هدرهای امنیتی)،
idempotency و پاک‌سازی فایل نیمه‌کاره، ارسال از طرف کارشناس.
"""
import io
import json
import os
import uuid
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.test import Client
from django.urls import reverse
from PIL import Image

from chat import attachments
from chat import conversations as conv
from chat.attachments import prepare_uploads
from chat.models import ChatAttachment, ChatMessage, Conversation
from chat.settingsio import cfg as load_cfg
from chat.testing import ChatTestBase, make_image, make_pdf, upload
from chat.tests_api import ApiBase, body
from chat.tests_console import ConsoleBase, jpost


def storage_files(root):
    found = []
    for folder, _, names in os.walk(os.path.join(root, 'chat_attachments')):
        found += [os.path.join(folder, n) for n in names]
    return found


class ValidationTests(ChatTestBase):
    def setUp(self):
        super().setUp()
        self.set(chat_attachments_enabled=True, chat_attachments_mode='images', chat_attachment_max_mb=1, chat_attachment_max_count=3)

    def prep(self, *files):
        return prepare_uploads(list(files), load_cfg())

    def code(self, *files):
        with self.assertRaises(conv.ChatError) as caught:
            self.prep(*files)
        return caught.exception.code

    def test_nothing_uploaded_means_nothing_prepared(self):
        self.assertEqual(self.prep(), [])

    def test_the_feature_is_off_by_default(self):
        self.set(chat_attachments_enabled=False)
        self.assertEqual(self.code(upload(make_image())), 'attachment_disabled')

    def test_real_jpeg_png_and_webp_are_accepted_and_typed_by_content(self):
        for fmt, ctype, ext in (('JPEG', 'image/jpeg', 'jpg'), ('PNG', 'image/png', 'png'), ('WEBP', 'image/webp', 'webp')):
            with self.subTest(fmt=fmt):
                item = self.prep(upload(make_image(fmt), name='whatever.bin', content_type='application/octet-stream'))[0]
                self.assertEqual((item.kind, item.content_type, item.ext), ('image', ctype, ext))
                self.assertEqual((item.width, item.height), (40, 30))

    def test_the_extension_and_content_type_are_never_trusted(self):
        self.assertEqual(self.code(upload(b'<?php system($_GET["c"]); ?>', name='shell.jpg', content_type='image/jpeg')), 'attachment_type')
        self.assertEqual(self.code(upload(b'<html><script>alert(1)</script>', name='x.png', content_type='image/png')), 'attachment_type')
        # PNG واقعی با پسوند و نوع جعلیِ دیگر پذیرفته می‌شود و پسوند ذخیره‌شده از محتواست
        self.assertEqual(self.prep(upload(make_image('PNG'), name='photo.exe', content_type='text/html'))[0].ext, 'png')

    def test_forbidden_formats_are_rejected(self):
        samples = {
            'svg': b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', 'zip': b'PK\x03\x04' + b'\0' * 40,
            'exe': b'MZ\x90\x00' + b'\0' * 60, 'gif': b'GIF89a' + b'\0' * 40, 'elf': b'\x7fELF' + b'\0' * 40, 'empty': b'',
        }
        for name, data in samples.items():
            with self.subTest(name=name):
                self.assertEqual(self.code(upload(data, name=f'f.{name}')), 'attachment_type')

    def test_corrupt_and_truncated_images_are_rejected(self):
        good = make_image('JPEG', (200, 200))
        self.assertEqual(self.code(upload(good[:len(good) // 2])), 'attachment_invalid')
        self.assertEqual(self.code(upload(b'\xff\xd8\xff' + b'garbage' * 20)), 'attachment_invalid')
        self.assertEqual(self.code(upload(make_image('PNG')[:60])), 'attachment_invalid')

    def test_exif_and_hidden_trailing_data_are_stripped_by_reencoding(self):
        raw = make_image('JPEG', exif=True, trailing=b'<?php evil(); ?>')
        self.assertIn(b'SECRET-GPS-123', raw)
        item = self.prep(upload(raw))[0]
        self.assertNotIn(b'SECRET-GPS-123', item.data)
        self.assertNotIn(b'SECRET-CAMERA', item.data)
        self.assertNotIn(b'<?php', item.data)
        self.assertEqual(dict(Image.open(io.BytesIO(item.data)).getexif()), {})

    def test_exif_orientation_is_applied_before_it_is_dropped(self):
        item = self.prep(upload(make_image('JPEG', (40, 20), orientation=6)))[0]       # ۹۰ درجه
        self.assertEqual((item.width, item.height), (20, 40))

    def test_big_images_are_shrunk_and_decompression_bombs_refused(self):
        item = self.prep(upload(make_image('PNG', (3000, 1000))))[0]
        self.assertEqual(max(item.width, item.height), attachments.MAX_SIDE)
        with mock.patch('chat.attachments.MAX_PIXELS', 1000):
            self.assertEqual(self.code(upload(make_image('PNG', (60, 60)))), 'attachment_invalid')

    def test_size_and_count_limits(self):
        self.assertEqual(self.code(upload(make_image('JPEG') + b'\0' * (1024 * 1024 + 10))), 'attachment_size')
        pics = [upload(make_image(), name=f'{i}.jpg') for i in range(4)]
        self.assertEqual(self.code(*pics), 'attachment_count')
        self.assertEqual(len(self.prep(*[upload(make_image(), name=f'{i}.jpg') for i in range(3)])), 3)

    def test_pdf_needs_the_documents_mode(self):
        self.assertEqual(self.code(upload(make_pdf(), name='a.pdf')), 'attachment_type')
        self.set(chat_attachments_mode='images_docs')
        item = self.prep(upload(make_pdf(), name='a.pdf', content_type='application/pdf'))[0]
        self.assertEqual((item.kind, item.content_type, item.ext), ('pdf', 'application/pdf', 'pdf'))

    def test_active_or_broken_pdfs_are_rejected(self):
        self.set(chat_attachments_mode='images_docs')
        bad = {
            'javascript': make_pdf(b'<< /S /JavaScript /JS (app.alert(1)) >>'), 'hex-obfuscated': make_pdf(b'<< /S /J#61vaScr#69pt >>'),
            'launch': make_pdf(b'<< /S /Launch /F (cmd.exe) >>'), 'embedded': make_pdf(b'<< /Type /EmbeddedFile >>'),
            'openaction': make_pdf(b'<< /OpenAction 1 0 R >>'), 'encrypted': make_pdf(b'<< /Encrypt 2 0 R >>'),
            'no-eof': make_pdf(eof=False),
        }
        for name, data in bad.items():
            with self.subTest(name=name):
                self.assertEqual(self.code(upload(data, name='a.pdf')), 'attachment_invalid')

    def test_the_display_name_is_sanitized_and_never_a_path(self):
        item = self.prep(upload(make_image(), name='../../etc/pass<wd>.jpg'))[0]
        self.assertEqual(item.name, 'passwd.jpg')
        self.assertEqual(attachments.display_name('C:\\temp\\a|b?.png'), 'ab.png')
        self.assertEqual(attachments.display_name(''), 'فایل')

    def test_accept_attribute_follows_the_mode(self):
        self.assertNotIn('pdf', attachments.accept_attribute(load_cfg()))
        self.set(chat_attachments_mode='images_docs')
        self.assertIn('application/pdf', attachments.accept_attribute(load_cfg()))


class CustomerFlowTests(ApiBase):
    def setUp(self):
        super().setUp()
        self.set(chat_attachments_enabled=True, chat_attachments_mode='images_docs', chat_attachment_max_count=3)
        self.client, response = self.create()
        self.cid = body(response)['conversation']['id']
        self.conversation = Conversation.objects.get()

    def send(self, files, text='', client=None, **extra):
        data = {'body': text, 'client_msg_id': str(uuid.uuid4()), 'files': files}
        data.update(extra)
        return (client or self.client).post(reverse('chat:send', args=[self.cid]), data)

    def test_tests_never_touch_the_real_media_folder(self):
        self.assertEqual(os.path.normcase(str(settings.MEDIA_ROOT)), os.path.normcase(self.media_root))
        self.assertNotEqual(os.path.normcase(str(settings.MEDIA_ROOT)), os.path.normcase(str(settings.BASE_DIR / 'media')))

    def test_an_image_message_is_stored_privately_under_a_uuid_path(self):
        response = self.send([upload(make_image('JPEG', exif=True), name='My Secret Name.jpg')], 'عکس کفش')
        self.assertEqual(response.status_code, 201)
        data = body(response)
        item = data['message']['attachments'][0]
        self.assertEqual((item['kind'], item['name'], item['type']), ('image', 'My Secret Name.jpg', 'image/jpeg'))
        self.assertIn(f'/chat/c/{self.cid}/files/{item["id"]}/', item['url'])
        files = storage_files(self.media_root)
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].startswith(os.path.join(self.media_root, 'chat_attachments')))
        self.assertRegex(os.path.basename(files[0]), rf'^{item["id"]}\.jpg$')
        self.assertNotIn(b'SECRET-GPS-123', open(files[0], 'rb').read())
        self.assertNotIn('Secret', files[0])
        with self.assertRaises(ValueError):
            ChatAttachment.objects.get().file.url                             # نشانی عمومی وجود ندارد

    def test_a_message_with_only_a_file_is_allowed_and_previewed(self):
        response = self.send([upload(make_image())])
        self.assertEqual(response.status_code, 201)
        self.assertEqual(ChatMessage.objects.last().body, '')
        self.assertEqual(Conversation.objects.get().last_message_preview, '📎 پیوست')

    def test_empty_text_without_a_file_is_still_rejected(self):
        self.assertEqual(body(self.send([]))['code'], 'empty')

    def test_validation_errors_use_the_error_envelope_and_leave_no_trace(self):
        before = ChatMessage.objects.count()
        for code, files in (('attachment_type', [upload(b'MZ' + b'\0' * 50, name='a.exe')]),
                            ('attachment_count', [upload(make_image(), name=f'{i}.jpg') for i in range(4)])):
            with self.subTest(code=code):
                response = self.send(files, 'متن')
                self.assertEqual((response.status_code, body(response)['code']), (400, code))
        self.assertEqual(ChatMessage.objects.count(), before)
        self.assertEqual(storage_files(self.media_root), [])

    def test_disabled_attachments_refuse_files_but_plain_text_still_works(self):
        self.set(chat_attachments_enabled=False)
        response = self.send([upload(make_image())], 'متن')
        self.assertEqual((response.status_code, body(response)['code']), (403, 'attachment_disabled'))
        self.assertEqual(self.send([], 'فقط متن').status_code, 201)

    def test_the_owner_downloads_the_file_with_hardened_headers(self):
        item = body(self.send([upload(make_image())]))['message']['attachments'][0]
        response = self.client.get(item['url'])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/jpeg')
        self.assertTrue(response['Content-Disposition'].startswith('inline'))
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertIn('sandbox', response['Content-Security-Policy'])
        self.assertIn('private', response['Cache-Control'])
        Image.open(io.BytesIO(b''.join(response.streaming_content))).verify()

    def test_pdfs_are_always_downloads_never_inline(self):
        item = body(self.send([upload(make_pdf(), name='invoice.pdf', content_type='application/pdf')]))['message']['attachments'][0]
        response = self.client.get(item['url'])
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertTrue(response['Content-Disposition'].startswith('attachment'))
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')

    def test_everyone_else_gets_the_same_404(self):
        item = body(self.send([upload(make_image())]))['message']['attachments'][0]
        stranger = Client()
        other_user = Client()
        other_user.force_login(self.make_user())
        for who in (stranger, other_user):
            self.assertEqual(who.get(item['url']).status_code, 404)
        self.assertEqual(self.client.get(f'/chat/c/{self.cid}/files/{uuid.uuid4()}/').status_code, 404)
        other_cid = uuid.uuid4()
        self.assertEqual(self.client.get(f'/chat/c/{other_cid}/files/{item["id"]}/').status_code, 404)

    def test_a_file_cannot_be_fetched_through_another_conversation(self):
        item = body(self.send([upload(make_image())]))['message']['attachments'][0]
        other_client, other = self.create(name='دیگری', client_msg_id=str(uuid.uuid4()))
        other_cid = body(other)['conversation']['id']
        self.assertEqual(other_client.get(f'/chat/c/{other_cid}/files/{item["id"]}/').status_code, 404)

    def test_polling_returns_attachments_and_only_costs_a_query_when_there_are_some(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self.send([], 'فقط متن')
        with CaptureQueriesContext(connection) as plain:
            data = body(self.client.get(reverse('chat:messages', args=[self.cid]) + '?after=0'))
        self.assertEqual(data['messages'][-1]['attachments'], [])
        self.send([upload(make_image())])
        with CaptureQueriesContext(connection) as with_files:
            data = body(self.client.get(reverse('chat:messages', args=[self.cid]) + '?after=0'))
        self.assertEqual(len(data['messages'][-1]['attachments']), 1)
        self.assertEqual(len(with_files), len(plain) + 1)

    def test_resending_the_same_message_stores_one_file(self):
        key = str(uuid.uuid4())
        first = self.send([upload(make_image())], 'عکس', client_msg_id=key)
        again = self.send([upload(make_image())], 'عکس', client_msg_id=key)
        self.assertEqual((first.status_code, again.status_code), (201, 200))
        self.assertEqual(ChatAttachment.objects.count(), 1)
        self.assertEqual(len(storage_files(self.media_root)), 1)

    def test_a_failure_midway_rolls_back_the_message_and_deletes_written_files(self):
        real_save = ChatAttachment.save
        calls = {'n': 0}

        def flaky(instance, *args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 2:
                raise RuntimeError('disk full')
            return real_save(instance, *args, **kwargs)

        before = ChatMessage.objects.count()
        with mock.patch.object(ChatAttachment, 'save', flaky):
            with self.assertRaises(RuntimeError):
                self.send([upload(make_image(), name='1.jpg'), upload(make_image('PNG'), name='2.png')], 'دو عکس')
        self.assertEqual(ChatMessage.objects.count(), before)
        self.assertEqual(ChatAttachment.objects.count(), 0)
        self.assertEqual(storage_files(self.media_root), [])
        self.assertEqual(Conversation.objects.get().last_message_seq, 1)             # seq هم برنگشته‌ی نیمه‌کاره نیست

    def test_rate_limit_is_checked_before_the_expensive_decode(self):
        self.set(chat_rate_limit_per_minute=1)
        self.send([], 'اولی')
        with mock.patch('chat.attachments.Image.open') as opened:
            response = self.send([upload(make_image())], 'دومی')
        self.assertEqual(response.status_code, 429)
        opened.assert_not_called()

    def test_the_per_conversation_attachment_cap(self):
        with mock.patch('chat.conversations.MAX_ATTACHMENTS_PER_CONVERSATION', 1):
            self.assertEqual(self.send([upload(make_image())]).status_code, 201)
            response = self.send([upload(make_image())])
        self.assertEqual((response.status_code, body(response)['code']), (400, 'attachment_count'))

    def test_internal_notes_never_carry_files_and_hidden_attachments_are_not_served(self):
        op = self.make_operator()
        note, _ = conv.post_message(self.conversation, sender='operator', body='یادداشت', operator=op, internal=True)
        with self.assertRaises(conv.ChatError):
            conv.post_message(self.conversation, sender='operator', body='x', operator=op, internal=True,
                              uploads=prepare_uploads([upload(make_image())], load_cfg()))
        # حتی اگر پیوستی به یادداشت برسد، دانلود مشتری ۴۰۴ است
        stray = ChatAttachment(message=note, kind='image', ext='jpg', content_type='image/jpeg', original_name='x.jpg', size=1)
        stray.file.save(f'{stray.public_id}.jpg', io.BytesIO(make_image()), save=False)
        stray.save()
        self.assertEqual(self.client.get(f'/chat/c/{self.cid}/files/{stray.public_id}/').status_code, 404)


class OperatorFlowTests(ConsoleBase, ApiBase):
    def setUp(self):
        ConsoleBase.setUp(self)
        self.set(chat_attachments_enabled=True, chat_attachments_mode='images_docs')
        self.client, response = self.create()
        self.cid = body(response)['conversation']['id']
        self.conversation = Conversation.objects.get()

    def reply(self, files, text='', **extra):
        data = {'body': text, 'client_msg_id': str(uuid.uuid4()), 'files': files}
        data.update(extra)
        return self.op_client.post(self.url('reply', self.conversation.pk), data)

    def test_the_operator_sends_an_image_and_the_customer_sees_and_downloads_it(self):
        response = self.reply([upload(make_image())], 'عکس محصول')
        self.assertEqual(response.status_code, 201)
        item = json.loads(response.content)['message']['attachments'][0]
        self.assertIn('/admin/chat/conversation/console/api/file/', item['url'])
        self.assertEqual(self.op_client.get(item['url']).status_code, 200)
        polled = body(self.client.get(reverse('chat:messages', args=[self.cid]) + '?after=0'))
        customer_item = polled['messages'][-1]['attachments'][0]
        self.assertIn(f'/chat/c/{self.cid}/files/', customer_item['url'])
        self.assertEqual(self.client.get(customer_item['url']).status_code, 200)

    def test_only_operators_can_open_files_through_the_console(self):
        item = json.loads(self.reply([upload(make_image())]).content)['message']['attachments'][0]
        staff = Client()
        staff.force_login(self.make_user(is_staff=True))
        self.assertEqual(staff.get(item['url']).status_code, 403)
        self.assertEqual(Client().get(item['url']).status_code, 302)
        self.assertEqual(self.op_client.get(f'/admin/chat/conversation/console/api/file/{uuid.uuid4()}/').status_code, 404)

    def test_the_operator_sees_the_customers_files_in_the_detail_api(self):
        client = self.client
        client.post(reverse('chat:send', args=[self.cid]), {'body': '', 'client_msg_id': str(uuid.uuid4()), 'files': [upload(make_image())]})
        detail = json.loads(self.op_client.get(self.url('detail', self.conversation.pk)).content)
        item = detail['messages'][-1]['attachments'][0]
        self.assertEqual(self.op_client.get(item['url']).status_code, 200)

    def test_a_note_with_a_file_stores_only_the_note(self):
        response = self.reply([upload(make_image())], 'یادداشت', note='1')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(ChatAttachment.objects.count(), 0)

    def test_operator_upload_errors(self):
        response = self.reply([upload(b'MZ' + b'\0' * 60, name='x.exe')], 'متن')
        self.assertEqual((response.status_code, json.loads(response.content)['code']), (400, 'attachment_type'))

    def test_removing_a_message_deletes_its_files_from_disk(self):
        self.reply([upload(make_image())])
        self.assertEqual(len(storage_files(self.media_root)), 1)
        with self.captureOnCommitCallbacks(execute=True):                    # django_cleanup بعد از commit فایل را پاک می‌کند
            Conversation.objects.all().delete()
        self.assertEqual(storage_files(self.media_root), [])


class ConfigTests(ChatTestBase):
    def test_the_widget_config_exposes_attachment_rules_and_not_paths(self):
        self.set(chat_attachments_enabled=True, chat_attachments_mode='images_docs', chat_attachment_max_mb=3, chat_attachment_max_count=2)
        data = json.loads(Client().get(reverse('chat:config')).content)
        self.assertEqual(data['attachments'], {'enabled': True, 'accept': 'image/jpeg,image/png,image/webp,application/pdf',
                                               'max_mb': 3, 'max_count': 2, 'allow_pdf': True})
        self.assertNotIn('chat_attachments', json.dumps(data))
        self.assertIn('blocked', data['texts'])
