"""
فاز ۴ - مسدودسازی (ChatBlock)، پاک‌سازی دوره‌ای (retention)، صفحه‌ی «پشتیبانی» پنل کاربری و اتصال گفتگوی مهمان به حساب.
"""
import json
import uuid
from datetime import timedelta
from unittest import mock

from django.contrib.auth.signals import user_logged_in
from django.core.cache import cache
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from chat import blocks, retention
from chat import conversations as conv
from chat import statemachine as sm
from chat.models import ChatAttachment, ChatBlock, ChatEvent, ChatMessage, Conversation
from chat.testing import ChatTestBase, make_image, upload
from chat.tests_api import ApiBase, body, post
from chat.tests_console import ConsoleBase, jget, jpost


class BlockTests(ConsoleBase, ApiBase):
    def setUp(self):
        ConsoleBase.setUp(self)
        self.client, response = self.create()
        self.cid = body(response)['conversation']['id']
        self.conversation = Conversation.objects.get()

    def block(self, **payload):
        data = {'action': 'block'}
        data.update(payload)
        return jpost(self.op_client, self.url('block', self.conversation.pk), data)

    def test_a_blocked_visitor_cannot_start_or_continue_conversations(self):
        self.assertEqual(self.block(reason='توهین').status_code, 200)
        self.assertEqual(ChatBlock.objects.get().reason, 'توهین')
        _, new = self.create(self.client, message='دوباره')
        self.assertEqual((new.status_code, body(new)['code']), (403, 'blocked'))
        self.assertEqual(body(new)['message'], self.cfg().chat_blocked_message)
        sent = post(self.client, reverse('chat:send', args=[self.cid]), {'body': 'x', 'client_msg_id': str(uuid.uuid4())})
        self.assertEqual((sent.status_code, body(sent)['code']), (403, 'blocked'))
        self.assertEqual(ChatMessage.objects.count(), 1)

    def cfg(self):
        from chat.settingsio import cfg
        return cfg()

    def test_a_blocked_visitor_can_still_read_history_and_sees_the_flag(self):
        self.block()
        self.assertEqual(self.client.get(reverse('chat:messages', args=[self.cid])).status_code, 200)
        self.assertTrue(body(self.client.get(reverse('chat:state')))['blocked'])
        self.assertFalse(body(Client().get(reverse('chat:state')))['blocked'])

    def test_blocking_does_not_touch_other_visitors(self):
        self.block()
        other, response = self.create(Client(), message='من یکی دیگرم')
        self.assertEqual(response.status_code, 201)

    def test_blocking_a_registered_user_follows_the_account_across_browsers(self):
        user = self.make_user()
        mine = Client()
        mine.force_login(user)
        _, response = self.create(mine, message='سلام')
        c = Conversation.objects.get(user=user)
        jpost(self.op_client, self.url('block', c.pk), {'action': 'block'})
        elsewhere = Client()
        elsewhere.force_login(user)
        _, again = self.create(elsewhere, message='از مرورگر دیگر')
        self.assertEqual((again.status_code, body(again)['code']), (403, 'blocked'))

    def test_a_timed_block_expires(self):
        data = json.loads(self.block(hours=2).content)
        self.assertTrue(data['blocked'])
        row = ChatBlock.objects.get()
        self.assertAlmostEqual((row.expires_at - timezone.now()).total_seconds(), 7200, delta=30)
        self.assertEqual(self.create(self.client, message='x')[1].status_code, 403)
        ChatBlock.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.create(self.client, message='حالا آزاد است')[1].status_code, 201)

    def test_unblocking_restores_access_and_blocking_twice_reuses_the_row(self):
        self.block(hours=1)
        self.block(reason='بار دوم')
        self.assertEqual(ChatBlock.objects.count(), 1)
        self.assertIsNone(ChatBlock.objects.get().expires_at)                  # دومی دائمی
        self.assertFalse(json.loads(jpost(self.op_client, self.url('block', self.conversation.pk), {'action': 'unblock'}).content)['blocked'])
        self.assertEqual(self.create(self.client, message='آزاد شدم')[1].status_code, 201)

    def test_blocking_can_close_the_open_conversation(self):
        data = json.loads(self.block(close=True).content)
        self.assertEqual(data['conversation']['status'], sm.CLOSED)
        event = ChatEvent.objects.filter(conversation=self.conversation, type='T17').get()
        self.assertEqual(event.meta, {'reason': 'blocked'})

    def test_the_detail_api_reports_the_block_state(self):
        self.assertFalse(jget(self.op_client, self.url('detail', self.conversation.pk))['blocked'])
        self.block()
        self.assertTrue(jget(self.op_client, self.url('detail', self.conversation.pk))['blocked'])

    def test_only_operators_can_block_and_input_is_validated(self):
        staff = Client()
        staff.force_login(self.make_user(is_staff=True))
        self.assertEqual(jpost(staff, self.url('block', self.conversation.pk), {'action': 'block'}).status_code, 403)
        self.assertEqual(ChatBlock.objects.count(), 0)
        for bad in ({'action': 'nuke'}, {'action': 'block', 'hours': 'x'}, {'action': 'block', 'hours': 99999}):
            with self.subTest(bad=bad):
                self.assertEqual(jpost(self.op_client, self.url('block', self.conversation.pk), bad).status_code, 400)
        self.assertEqual(ChatBlock.objects.count(), 0)

    def test_typing_from_a_blocked_visitor_is_ignored(self):
        self.block()
        post(self.client, reverse('chat:typing', args=[self.cid]))
        self.assertFalse(jget(self.op_client, self.url('detail', self.conversation.pk))['typing'])

    def test_helper_identity_checks(self):
        self.assertFalse(blocks.is_blocked_identity('', None))
        self.block()
        self.assertTrue(blocks.is_blocked_identity(self.conversation.visitor_hash, None))
        self.assertFalse(blocks.is_blocked_identity('z' * 64, None))

    def test_the_admin_list_is_visible_to_operators_and_not_addable(self):
        self.block()
        self.assertEqual(self.op_client.get(reverse('admin:chat_chatblock_changelist')).status_code, 200)
        self.assertEqual(self.op_client.get(reverse('admin:chat_chatblock_add')).status_code, 403)
        row = ChatBlock.objects.get()
        self.assertEqual(self.op_client.post(reverse('admin:chat_chatblock_delete', args=[row.pk]), {'post': 'yes'}).status_code, 403)
        self.assertEqual(ChatBlock.objects.count(), 1)


class RetentionTests(ChatTestBase):
    def closed(self, days_ago, visitor, **extra):
        c = self.new_conversation(visitor=visitor, status=sm.CLOSED, closed_at=timezone.now() - timedelta(days=days_ago), **extra)
        return c

    def test_zero_days_keeps_everything_forever(self):
        self.closed(900, 'a' * 40)
        self.assertEqual(retention.purge_expired(), 0)
        self.assertEqual(Conversation.objects.count(), 1)

    def test_only_old_closed_conversations_go_with_everything_attached(self):
        self.set(chat_retention_days=30)
        old = self.closed(45, 'a' * 40)
        recent = self.closed(10, 'b' * 40)
        still_open = self.new_conversation(visitor='c' * 40, status=sm.ACTIVE, last_activity_at=timezone.now() - timedelta(days=400))
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(retention.purge_expired(), 1)
        self.assertEqual(sorted(Conversation.objects.values_list('pk', flat=True)), sorted([recent.pk, still_open.pk]))
        self.assertFalse(ChatMessage.objects.filter(conversation=old).exists())
        self.assertFalse(ChatEvent.objects.filter(conversation=old).exists())

    def test_files_of_purged_conversations_leave_the_disk(self):
        import os

        from chat.attachments import prepare_uploads
        from chat.settingsio import cfg
        self.set(chat_retention_days=30, chat_attachments_enabled=True)
        old = self.new_conversation(visitor='a' * 40)
        conv.post_message(old, sender='customer', body='عکس', uploads=prepare_uploads([upload(make_image())], cfg()))
        Conversation.objects.filter(pk=old.pk).update(status=sm.CLOSED, closed_at=timezone.now() - timedelta(days=60))
        folder = os.path.join(self.media_root, 'chat_attachments')
        self.assertTrue(any(files for _, _, files in os.walk(folder)))
        with self.captureOnCommitCallbacks(execute=True):
            retention.purge_expired()
        self.assertEqual(ChatAttachment.objects.count(), 0)
        self.assertFalse(any(files for _, _, files in os.walk(folder)))

    def test_blocks_survive_the_conversation_they_came_from(self):
        self.set(chat_retention_days=30)
        old = self.closed(60, 'a' * 40)
        ChatBlock.objects.create(visitor_hash='a' * 40, conversation=old, reason='x')
        retention.purge_expired()
        block = ChatBlock.objects.get()
        self.assertIsNone(block.conversation_id)
        self.assertTrue(blocks.is_blocked_identity('a' * 40, None))

    def test_deleting_runs_in_batches_and_a_bad_batch_stops_the_run_safely(self):
        self.set(chat_retention_days=30)
        for i in range(5):
            self.closed(60, f'{i:02d}' * 20)
        with mock.patch('chat.retention.BATCH', 2):
            self.assertEqual(retention.purge_expired(), 5)
        for i in range(3):
            self.closed(60, f'{i + 10:02d}' * 20)
        with mock.patch('django.db.models.query.QuerySet.delete', side_effect=RuntimeError('db')), self.assertLogs('chat.retention', 'ERROR'):
            self.assertEqual(retention.purge_expired(), 0)
        self.assertEqual(Conversation.objects.count(), 3)

    def test_the_celery_task_and_nightly_schedule(self):
        from chat.tasks import purge_expired_chats
        from config.celery import app, setup_chat_retention_schedule

        self.set(chat_retention_days=30)
        self.closed(60, 'a' * 40)
        self.assertEqual(purge_expired_chats(), 1)
        app.loader.import_default_modules()
        self.assertIn('chat.tasks.purge_expired_chats', app.tasks)
        sender = mock.Mock()
        sender.signature.side_effect = lambda name: name
        setup_chat_retention_schedule(sender)
        args, kwargs = sender.add_periodic_task.call_args
        self.assertEqual((args[1], kwargs['name']), ('chat.tasks.purge_expired_chats', 'purge-expired-chats'))
        self.assertEqual((args[0].hour, args[0].minute), ({3}, {40}))


class SupportPageTests(ChatTestBase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user(first_name='رضا')
        self.client = Client()
        self.client.force_login(self.user)

    def test_login_is_required(self):
        for url in (reverse('chat:support'), reverse('chat:support_detail', args=[uuid.uuid4()])):
            response = Client().get(url)
            self.assertEqual(response.status_code, 302)
            self.assertIn('/accounts/login/', response['Location'])

    def test_the_page_lists_only_my_conversations_with_two_independent_tabs(self):
        mine = self.new_conversation(user=self.user, body='سؤال من', visitor='a' * 40)
        other = self.new_conversation(user=self.make_user(), body='سؤال دیگری', visitor='b' * 40)
        guest = self.new_conversation(body='سؤال مهمان', visitor='c' * 40)
        html = self.client.get(reverse('chat:support')).content.decode()
        self.assertIn('سؤال من', html)
        self.assertNotIn('سؤال دیگری', html)
        self.assertNotIn('سؤال مهمان', html)
        self.assertIn(reverse('chat:support_detail', args=[mine.public_id]), html)
        self.assertIn('گفتگوها', html)
        self.assertIn('تیکت‌ها', html)
        tickets = self.client.get(reverse('chat:support') + '?tab=tickets').content.decode()
        self.assertIn('تیکت‌های پشتیبانی به‌زودی', tickets)
        self.assertNotIn('سؤال من', tickets)                                   # تیکت و گفتگو درهم‌تنیده نیستند

    def test_an_empty_list_has_a_helpful_state(self):
        self.assertIn('هنوز گفتگویی ندارید', self.client.get(reverse('chat:support')).content.decode())

    def test_the_transcript_hides_internal_notes_shows_files_and_marks_read(self):
        from chat.attachments import prepare_uploads
        from chat.settingsio import cfg

        self.set(chat_attachments_enabled=True)
        op = self.make_operator()
        c = self.new_conversation(user=self.user, body='سلام')
        conv.post_message(c, sender='operator', body='یادداشت محرمانه', operator=op, internal=True)
        conv.post_message(c, sender='operator', body='پاسخ شما', operator=op, uploads=prepare_uploads([upload(make_image())], cfg()))
        self.assertEqual(self.reload(c).unread_for_customer, 1)
        html = self.client.get(reverse('chat:support_detail', args=[c.public_id])).content.decode()
        self.assertIn('پاسخ شما', html)
        self.assertNotIn('یادداشت محرمانه', html)
        att = ChatAttachment.objects.get()
        file_url = reverse('chat:file', args=[c.public_id, att.public_id])
        self.assertIn(file_url, html)
        self.assertEqual(self.client.get(file_url).status_code, 200)
        self.assertEqual(self.reload(c).unread_for_customer, 0)

    def test_someone_elses_conversation_is_a_404(self):
        theirs = self.new_conversation(user=self.make_user(), visitor='b' * 40)
        guest = self.new_conversation(visitor='c' * 40)
        for c in (theirs, guest):
            self.assertEqual(self.client.get(reverse('chat:support_detail', args=[c.public_id])).status_code, 404)

    def test_pagination(self):
        now = timezone.now()
        for i in range(17):
            Conversation.objects.create(user=self.user, status=sm.CLOSED, closed_at=now, last_message_at=now - timedelta(minutes=i),
                                        last_message_preview=f'پیام شماره {i}', last_activity_at=now)
        first = self.client.get(reverse('chat:support')).content.decode()
        self.assertIn('پیام شماره 0', first)
        self.assertNotIn('پیام شماره 16', first)
        self.assertIn('?page=2', first)
        second = self.client.get(reverse('chat:support') + '?page=2').content.decode()
        self.assertIn('پیام شماره 16', second)
        self.assertEqual(self.client.get(reverse('chat:support') + '?page=999').status_code, 200)    # صفحه‌ی خارج از محدوده خراب نمی‌شود

    def test_the_panel_menu_links_to_support(self):
        self.assertIn(reverse('chat:support'), self.client.get(reverse('accounts:dashboard')).content.decode())


class GuestLinkingTests(ApiBase):
    def test_a_guest_conversation_appears_in_the_support_page_after_login(self):
        guest, response = self.create(message='پیام قبل از ورود')
        c = Conversation.objects.get()
        self.assertIsNone(c.user_id)
        user = self.make_user()
        # ورود واقعی: درخواستِ دارای کوکی بازدیدکننده‌ی همان مرورگر، سیگنال user_logged_in را می‌فرستد
        request = RequestFactory().get('/')
        request.COOKIES['chat_vid'] = guest.cookies['chat_vid'].value
        user_logged_in.send(sender=user.__class__, request=request, user=user)
        guest.force_login(user)
        c.refresh_from_db()
        self.assertEqual(c.user_id, user.pk)
        html = guest.get(reverse('chat:support')).content.decode()
        self.assertIn('پیام قبل از ورود', html)
        self.assertTrue(ChatEvent.objects.filter(conversation=c, type='attached').exists())

    def test_another_browser_with_the_same_account_does_not_hijack_guest_conversations(self):
        guest, _ = self.create(message='مال مهمان')
        other_browser = Client()
        other_browser.force_login(self.make_user())
        self.assertIsNone(Conversation.objects.get().user_id)
