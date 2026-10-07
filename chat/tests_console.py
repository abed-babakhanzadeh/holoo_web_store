"""
پیشخوان کارشناس: دسترسی (گروه/مجوز)، صندوق و فیلترها، گفتگو و کارت مشتری، پاسخ/یادداشت/اقدام‌ها، پاسخ‌های آماده، ادمین؛
پیامک‌های گفتگو (کارشناسان و مشتری) با cooldown و قاعده‌ی مهمانِ تأییدنشده.
"""
import json
import uuid
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from chat import cache as chatcache
from chat import conversations as conv
from chat import statemachine as sm
from chat.apps import OPERATOR_GROUP_NAME, ensure_operator_group
from chat.models import ChatMessage, Conversation, QuickReply
from chat.testing import ChatTestBase
from notifications.models import Notification, NotificationSetting, sync_notification_settings


def jpost(client, url, data=None):
    return client.post(url, data=json.dumps(data or {}), content_type='application/json')


def jget(client, url):
    return json.loads(client.get(url).content)


class ConsoleBase(ChatTestBase):
    def setUp(self):
        super().setUp()
        self.op = self.make_operator()
        self.op_client = Client()
        self.op_client.force_login(self.op)

    def url(self, name, *args):
        return reverse(f'admin:chat_console_{name}', args=args)

    def callback(self):
        return self.captureOnCommitCallbacks(execute=True)


class AccessTests(ConsoleBase):
    def test_anonymous_users_are_sent_to_the_admin_login(self):
        response = Client().get(reverse('admin:chat_conversation_console'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/admin/login/', response['Location'])
        self.assertEqual(Client().get(self.url('inbox')).status_code, 302)

    def test_staff_without_the_permission_are_forbidden_everywhere(self):
        staff = self.make_user(is_staff=True)
        client = Client()
        client.force_login(staff)
        c = self.new_conversation()
        self.assertEqual(client.get(reverse('admin:chat_conversation_console')).status_code, 403)
        for name, args in (('inbox', ()), ('detail', (c.pk,)), ('quick_replies', ()), ('operators', ())):
            with self.subTest(name=name):
                response = client.get(self.url(name, *args))
                self.assertEqual(response.status_code, 403)
                self.assertEqual(json.loads(response.content)['code'], 'forbidden')
        for name in ('reply', 'read', 'action'):
            with self.subTest(name=name):
                self.assertEqual(jpost(client, self.url(name, c.pk), {'body': 'x', 'client_msg_id': str(uuid.uuid4()), 'action': 'close'}).status_code, 403)
        self.assertEqual(self.reload(c).status, sm.OFFLINE)

    def test_a_customer_account_cannot_reach_the_console_even_if_logged_in(self):
        client = Client()
        client.force_login(self.make_user())                                       # staff نیست ← ادمین او را به لاگین می‌فرستد
        self.assertEqual(client.get(self.url('inbox')).status_code, 302)

    def test_operators_and_superusers_are_allowed(self):
        self.assertEqual(self.op_client.get(reverse('admin:chat_conversation_console')).status_code, 200)
        boss = Client()
        boss.force_login(self.make_operator(superuser=True))
        self.assertEqual(boss.get(self.url('inbox')).status_code, 200)

    def test_the_operator_group_exists_with_the_permission(self):
        ensure_operator_group(sender=None)
        group = Group.objects.get(name=OPERATOR_GROUP_NAME)
        self.assertTrue(group.permissions.filter(codename='operate_chat').exists())
        ensure_operator_group(sender=None)                                         # idempotent
        self.assertEqual(Group.objects.filter(name=OPERATOR_GROUP_NAME).count(), 1)

    def test_an_inactive_operator_is_locked_out(self):
        self.op.is_active = False
        self.op.save()
        self.assertEqual(self.op_client.get(self.url('inbox')).status_code, 302)

    def test_console_page_ships_its_api_urls_and_assets(self):
        html = self.op_client.get(reverse('admin:chat_conversation_console')).content.decode()
        self.assertIn('chat-console.js', html)
        self.assertIn('chat-console.css', html)
        self.assertIn('id="cc-api"', html)

    def test_console_warns_when_chat_is_off(self):
        self.set(chat_enabled=False)
        self.assertIn('خاموش است', self.op_client.get(reverse('admin:chat_conversation_console')).content.decode())


class InboxTests(ConsoleBase):
    def test_filters_counts_and_search(self):
        waiting = self.new_conversation(status=sm.WAITING_OPERATOR, visitor='a' * 40, name='صف')
        offline = self.new_conversation(visitor='b' * 40, name='آفلاین')
        mine = self.new_conversation(status=sm.ACTIVE, assigned_operator=self.op, unread_for_operator=0, visitor='c' * 40, name='من')
        closed = self.new_conversation(status=sm.CLOSED, closed_at=timezone.now(), visitor='d' * 40, name='بسته')
        ids = lambda f, q='': [r['id'] for r in jget(self.op_client, self.url('inbox') + f'?filter={f}&q={q}')['conversations']]
        self.assertEqual(set(ids('all')), {waiting.pk, offline.pk, mine.pk})
        self.assertEqual(ids('closed'), [closed.pk])
        self.assertEqual(ids('mine'), [mine.pk])
        self.assertEqual(ids('offline'), [offline.pk])
        self.assertEqual(set(ids('unread')), {waiting.pk, offline.pk})
        self.assertEqual(set(ids('waiting')), {waiting.pk})
        self.assertEqual(ids('all', q='آفلاین'), [offline.pk])
        data = jget(self.op_client, self.url('inbox'))
        self.assertEqual(data['counts'], {'unread': 2, 'waiting': 1, 'offline': 1, 'mine': 1})

    def test_unknown_filter_falls_back_to_all(self):
        self.new_conversation()
        self.assertEqual(jget(self.op_client, self.url('inbox') + '?filter=hack')['filter'], 'all')

    def test_the_newest_activity_comes_first_and_rows_carry_the_preview(self):
        old = self.new_conversation(visitor='a' * 40, body='قدیمی')
        new = self.new_conversation(visitor='b' * 40, body='جدید')
        Conversation.objects.filter(pk=old.pk).update(last_message_at=timezone.now() - timedelta(hours=3))
        rows = jget(self.op_client, self.url('inbox'))['conversations']
        self.assertEqual([r['id'] for r in rows], [new.pk, old.pk])
        self.assertEqual(rows[0]['preview'], 'جدید')
        self.assertEqual(rows[0]['registered'], False)

    def test_the_list_is_capped(self):
        with mock.patch('chat.console.INBOX_LIMIT', 3):
            for i in range(5):
                self.new_conversation(visitor=f'{i:02d}' * 20)
            self.assertEqual(len(jget(self.op_client, self.url('inbox'))['conversations']), 3)

    def test_the_inbox_costs_a_constant_number_of_queries(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def count():
            with CaptureQueriesContext(connection) as queries:
                self.op_client.get(self.url('inbox'))
            return len(queries)

        for i in range(2):
            self.new_conversation(visitor=f'{i:02d}' * 20, user=self.make_user())
        few = count()
        for i in range(2, 12):
            self.new_conversation(visitor=f'{i:02d}' * 20, user=self.make_user() if i % 2 else None)
        self.assertEqual(count(), few)                       # N+1 ندارد


class DetailAndActionTests(ConsoleBase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user(first_name='رضا', last_name='احمدی')
        self.c = self.new_conversation(user=self.user, body='سلام')

    def test_detail_shows_messages_the_customer_card_and_the_allowed_actions(self):
        conv.post_message(self.c, sender='operator', body='فقط داخلی', operator=self.op, internal=True)
        data = jget(self.op_client, self.url('detail', self.c.pk) + '?after=0')
        self.assertEqual([m['body'] for m in data['messages']], ['سلام', 'فقط داخلی'])
        self.assertEqual([m['note'] for m in data['messages']], [False, True])                      # کارشناس یادداشت داخلی را می‌بیند
        card = data['card']
        self.assertEqual((card['name'], card['phone'], card['registered'], card['price_level']),
                         ('رضا احمدی', self.user.phone_number, True, 1))
        self.assertTrue(card['user_url'].startswith('/admin/accounts/customuser/'))
        self.assertEqual(card['orders_count'], 0)
        self.assertEqual(sorted(data['actions']), ['close'])                                         # OFFLINE: فقط بستن (پاسخ با reply)

    def test_the_card_lists_the_latest_orders(self):
        from orders.models import Order
        for i in range(4):
            Order.objects.create(user=self.user, first_name='ر', last_name='ا', phone=self.user.phone_number, address='x',
                                 payment_method='cash', total_price=1000 * (i + 1))
        card = jget(self.op_client, self.url('detail', self.c.pk))['card']
        self.assertEqual(card['orders_count'], 4)
        self.assertEqual(len(card['orders']), 3)
        self.assertEqual(card['orders'][0]['total'], 4000)

    def test_a_guest_card_has_no_account_data(self):
        guest = self.new_conversation(visitor='g' * 40, name='میهمان', phone='09125556666')
        card = jget(self.op_client, self.url('detail', guest.pk))['card']
        self.assertEqual((card['registered'], card['name'], card['phone'], card['phone_verified']), (False, 'میهمان', '09125556666', False))
        self.assertNotIn('orders', card)

    def test_unknown_conversation_is_404(self):
        self.assertEqual(self.op_client.get(self.url('detail', 999999)).status_code, 404)

    def test_the_cursor_works_in_the_console_too(self):
        self.customer_says(self.c, 'دوم')
        data = jget(self.op_client, self.url('detail', self.c.pk) + '?after=1')
        self.assertEqual([m['seq'] for m in data['messages']], [2])

    def test_replying_walks_the_matrix_and_registers_the_operator(self):
        key = str(uuid.uuid4())
        with self.callback():
            response = jpost(self.op_client, self.url('reply', self.c.pk), {'body': 'سلام رضا', 'client_msg_id': key})
        data = json.loads(response.content)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(data['conversation']['status'], sm.WAITING_CUSTOMER)                         # T13
        self.assertEqual(data['conversation']['assigned_id'], self.op.pk)
        again = jpost(self.op_client, self.url('reply', self.c.pk), {'body': 'سلام رضا', 'client_msg_id': key})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(ChatMessage.objects.filter(sender_type='operator').count(), 1)

    def test_a_note_is_stored_but_changes_nothing_for_the_customer(self):
        jpost(self.op_client, self.url('reply', self.c.pk), {'body': 'یادداشت', 'client_msg_id': str(uuid.uuid4()), 'note': True})
        c = self.reload(self.c)
        self.assertEqual((c.status, c.unread_for_customer), (sm.OFFLINE, 0))

    def test_replying_to_a_closed_conversation_is_refused(self):
        closed = self.new_conversation(visitor='z' * 40, status=sm.CLOSED, closed_at=timezone.now())
        response = jpost(self.op_client, self.url('reply', closed.pk), {'body': 'x', 'client_msg_id': str(uuid.uuid4())})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.content)['code'], 'closed')

    def test_bad_reply_input(self):
        for payload in ({'body': '   ', 'client_msg_id': str(uuid.uuid4())}, {'body': 'x', 'client_msg_id': 'نه'}, {'body': 'x' * 4001, 'client_msg_id': str(uuid.uuid4())}):
            with self.subTest(payload=payload):
                self.assertEqual(jpost(self.op_client, self.url('reply', self.c.pk), payload).status_code, 400)

    def test_every_action_goes_through_the_matrix(self):
        queue = self.new_conversation(visitor='q' * 40, status=sm.WAITING_OPERATOR)
        post = lambda cid, action, **extra: jpost(self.op_client, self.url('action', cid), {'action': action, **extra})
        self.assertEqual(json.loads(post(queue.pk, 'claim').content)['conversation']['status'], sm.ACTIVE)
        self.assertEqual(json.loads(post(queue.pk, 'waiting').content)['conversation']['status'], sm.WAITING_CUSTOMER)
        self.assertEqual(post(queue.pk, 'claim').status_code, 409)                                     # T3 از «منتظر مشتری» ممنوع
        self.assertEqual(json.loads(post(queue.pk, 'release').content)['conversation']['status'], sm.WAITING_OPERATOR)
        self.assertEqual(json.loads(post(queue.pk, 'close').content)['conversation']['status'], sm.CLOSED)
        self.assertEqual(post(queue.pk, 'close').status_code, 409)
        self.assertEqual(json.loads(post(queue.pk, 'reopen').content)['conversation']['status'], sm.WAITING_OPERATOR)
        self.assertEqual(post(queue.pk, 'explode').status_code, 400)

    def test_reassign_needs_a_real_operator(self):
        active = self.new_conversation(visitor='r' * 40, status=sm.ACTIVE, assigned_operator=self.op)
        other = self.make_operator()
        post = lambda target: jpost(self.op_client, self.url('action', active.pk), {'action': 'reassign', 'operator_id': target})
        self.assertEqual(post(self.make_user().pk).status_code, 400)                                    # عضو گروه نیست
        self.assertEqual(post(0).status_code, 400)
        self.assertEqual(json.loads(post(other.pk).content)['conversation']['assigned_id'], other.pk)

    def test_read_marks_the_operator_watermark(self):
        self.customer_says(self.c)
        data = json.loads(jpost(self.op_client, self.url('read', self.c.pk), {'upto_seq': 2}).content)
        self.assertEqual((data['unread'], data['last_read_seq']), (0, 2))

    def test_operators_listing_and_quick_replies_visibility(self):
        other = self.make_operator()
        QuickReply.objects.create(title='عمومی', body='متن', shortcut='g')
        QuickReply.objects.create(title='شخصی من', body='متن', owner=self.op)
        QuickReply.objects.create(title='شخصی دیگری', body='متن', owner=other)
        QuickReply.objects.create(title='غیرفعال', body='متن', is_active=False)
        titles = [q['title'] for q in jget(self.op_client, self.url('quick_replies'))['items']]
        self.assertEqual(sorted(titles), ['شخصی من', 'عمومی'])
        names = [o['id'] for o in jget(self.op_client, self.url('operators'))['items']]
        self.assertIn(self.op.pk, names)
        self.assertIn(other.pk, names)

    def test_using_a_quick_reply_counts_its_usage(self):
        quick = QuickReply.objects.create(title='سلام', body='سلام {customer_name}')
        jpost(self.op_client, self.url('reply', self.c.pk), {'body': 'سلام رضا', 'client_msg_id': str(uuid.uuid4()), 'quick_reply_id': quick.pk})
        quick.refresh_from_db()
        self.assertEqual(quick.usage_count, 1)


class AdminPagesTests(ConsoleBase):
    def test_changelist_shows_the_console_link_and_the_conversation_page_renders(self):
        boss = Client()
        boss.force_login(self.make_operator(superuser=True))
        c = self.new_conversation()
        self.customer_says(c, 'دوم')
        html = boss.get(reverse('admin:chat_conversation_changelist')).content.decode()
        self.assertIn(reverse('admin:chat_conversation_console'), html)
        self.assertEqual(boss.get(reverse('admin:chat_conversation_change', args=[c.pk])).status_code, 200)

    def test_operators_see_but_cannot_change_or_delete_conversations(self):
        c = self.new_conversation()
        self.assertEqual(self.op_client.get(reverse('admin:chat_conversation_changelist')).status_code, 200)
        self.assertEqual(self.op_client.post(reverse('admin:chat_conversation_delete', args=[c.pk]), {'post': 'yes'}).status_code, 403)
        self.assertTrue(Conversation.objects.filter(pk=c.pk).exists())

    def test_operators_manage_only_their_own_quick_replies(self):
        mine = QuickReply.objects.create(title='من', body='x', owner=self.op)
        theirs = QuickReply.objects.create(title='او', body='x', owner=self.make_operator())
        shared = QuickReply.objects.create(title='مشترک', body='x')
        html = self.op_client.get(reverse('admin:chat_quickreply_changelist')).content.decode()
        self.assertIn('من', html)
        self.assertNotIn('>او<', html)
        self.assertNotIn('>مشترک<', html)
        self.assertEqual(self.op_client.get(reverse('admin:chat_quickreply_change', args=[theirs.pk])).status_code, 302)   # دسترسی ندارد
        self.assertEqual(self.op_client.get(reverse('admin:chat_quickreply_change', args=[shared.pk])).status_code, 302)
        self.assertEqual(self.op_client.get(reverse('admin:chat_quickreply_change', args=[mine.pk])).status_code, 200)
        self.op_client.post(reverse('admin:chat_quickreply_add'), {'title': 'تازه', 'body': 'متن', 'shortcut': 't', 'is_active': 'on', 'sort_order': 0})
        self.assertEqual(QuickReply.objects.get(title='تازه').owner_id, self.op.pk)                                           # مالک خودکار

    def test_a_plain_customer_cannot_see_the_chat_admin(self):
        client = Client()
        client.force_login(self.make_user(is_staff=True))
        self.assertEqual(client.get(reverse('admin:chat_conversation_changelist')).status_code, 403)


class OperatorSmsTests(ConsoleBase):
    def setUp(self):
        super().setUp()
        sync_notification_settings()
        self.set(chat_enabled=True, chat_notify_phones='09121112222\n09123334444', chat_admin_sms_cooldown_minutes=10)
        patcher = mock.patch('notifications.tasks.deliver_notification.delay')
        patcher.start()
        self.addCleanup(patcher.stop)

    def sent(self, key):
        return list(Notification.objects.filter(template_key=key).order_by('id'))

    def new_offline(self, **kw):
        with self.callback():
            return self.new_conversation(**kw)

    def test_a_new_offline_message_alerts_every_configured_operator_phone_once_per_cooldown(self):
        c = self.new_offline(name='سارا')
        sms = self.sent('chat_offline_message_admin')
        self.assertEqual(sorted(n.recipient for n in sms), ['09121112222', '09123334444'])
        self.assertIn('سارا', sms[0].text)
        with self.callback():
            self.customer_says(c, 'پیام دوم')                                  # داخل cooldown
        self.assertEqual(len(self.sent('chat_offline_message_admin')), 2)
        cache.delete('chat:sms:admin:%s' % c.pk)                              # cooldown گذشت
        with self.callback():
            self.customer_says(c, 'پیام سوم')
        self.assertEqual(len(self.sent('chat_offline_message_admin')), 4)

    def test_each_conversation_has_its_own_cooldown(self):
        self.new_offline(visitor='a' * 40)
        self.new_offline(visitor='b' * 40)
        self.assertEqual(len(self.sent('chat_offline_message_admin')), 4)

    def test_without_configured_phones_the_site_default_recipient_is_used(self):
        self.set(chat_enabled=True, chat_notify_phones='')
        with self.settings(ADMIN_NOTIFICATION_RECIPIENT='09120009999'):
            self.new_offline()
        self.assertEqual([n.recipient for n in self.sent('chat_offline_message_admin')], ['09120009999'])

    def test_the_admin_can_switch_the_sms_off_in_the_notification_settings(self):
        NotificationSetting.objects.filter(template_key='chat_offline_message_admin').update(is_enabled=False)
        self.new_offline()
        self.assertEqual(self.sent('chat_offline_message_admin'), [])

    def test_user_supplied_names_cannot_inject_links_or_newlines_into_the_sms(self):
        self.new_offline(name='ببین http://evil.example/x\nخط دوم')
        text = self.sent('chat_offline_message_admin')[0].text
        self.assertNotIn('evil.example', text)
        self.assertNotIn('\n', text)

    def test_live_queue_messages_do_not_text_operators_in_phase_two(self):
        queue = self.new_offline(status=sm.WAITING_OPERATOR)
        before = len(self.sent('chat_offline_message_admin'))
        with self.callback():
            self.customer_says(queue)
        self.assertEqual(len(self.sent('chat_offline_message_admin')), before)

    def test_the_sms_cooldown_falls_back_to_the_database_when_redis_is_down(self):
        boom = mock.Mock(side_effect=OSError('down'))
        with mock.patch.multiple('chat.cache.cache', add=boom, incr=boom, get=boom, set=boom):
            c = self.new_offline()
            with self.callback():
                self.customer_says(c, 'دوم')
        self.assertEqual(len(self.sent('chat_offline_message_admin')), 2)             # فقط یک دور (دو شماره)، نه دو دور


class CustomerSmsTests(ConsoleBase):
    def setUp(self):
        super().setUp()
        sync_notification_settings()
        self.set(chat_enabled=True, chat_customer_sms_cooldown_minutes=30, chat_notify_phones='09121112222')
        patcher = mock.patch('notifications.tasks.deliver_notification.delay')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.user = self.make_user(first_name='رضا')

    def reply(self, c, text='جواب'):
        with self.callback():
            return self.operator_says(c, self.op, text)

    def sent(self):
        return list(Notification.objects.filter(template_key='chat_reply_customer'))

    def offline_for(self, user=None, **kw):
        with self.callback():
            return self.new_conversation(user=user, **kw)

    def test_a_logged_in_customer_gets_one_sms_per_cooldown_for_a_human_reply(self):
        c = self.offline_for(self.user)
        self.reply(c)
        sms = self.sent()
        self.assertEqual([n.recipient for n in sms], [self.user.phone_number])
        self.assertIn('رضا', sms[0].text)
        self.reply(c, 'پیگیری')                                                    # T14 داخل cooldown
        self.assertEqual(len(self.sent()), 1)
        cache.delete('chat:sms:cust:%s' % c.pk)
        self.reply(c, 'باز هم')
        self.assertEqual(len(self.sent()), 2)

    def test_no_sms_while_the_customer_is_looking_at_the_conversation(self):
        c = self.offline_for(self.user)
        chatcache.mark_customer_seen(c.pk)
        self.reply(c)
        self.assertEqual(self.sent(), [])

    def test_an_unverified_guest_never_receives_an_sms(self):
        c = self.offline_for(name='مهمان', phone='09125557777')
        self.reply(c)
        self.assertEqual(self.sent(), [])

    def test_a_verified_guest_phone_may_receive_it(self):
        c = self.offline_for(name='مهمان', phone='09125557777', guest_phone_verified=True)
        self.reply(c)
        self.assertEqual([n.recipient for n in self.sent()], ['09125557777'])

    def test_the_admin_can_switch_the_customer_sms_off(self):
        NotificationSetting.objects.filter(template_key='chat_reply_customer').update(is_enabled=False)
        c = self.offline_for(self.user)
        self.reply(c)
        self.assertEqual(self.sent(), [])

    def test_only_replies_to_async_conversations_trigger_it(self):
        c = self.offline_for(self.user, status=sm.WAITING_OPERATOR)
        self.reply(c)                                                              # T3 (صف): پیامک نمی‌رود
        self.assertEqual(self.sent(), [])

    def test_the_two_new_templates_are_in_the_notification_settings(self):
        rows = set(NotificationSetting.objects.filter(template_key__startswith='chat_').values_list('template_key', 'is_enabled'))
        self.assertEqual(rows, {('chat_offline_message_admin', True), ('chat_reply_customer', True)})
