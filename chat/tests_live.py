"""
فاز ۳ - حضور کارشناس، دروازه‌ی گفتگوی زنده (سه وضعیت)، شروع گفتگوی زنده (T1/T11)، API حضور در پیشخوان، نشانگر «در حال نوشتن».
"""
import json
import uuid
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from chat import availability, presence
from chat import cache as chatcache
from chat import conversations as conv
from chat import statemachine as sm
from chat.models import ChatMessage, Conversation, OperatorPresence
from chat.tests_api import ApiBase, body, post
from chat.tests_console import ConsoleBase, jget, jpost
from chat.testing import ChatTestBase
from products.chat_settings import CHAT_CFG_CACHE_KEY


class PresenceTests(ChatTestBase):
    def setUp(self):
        super().setUp()
        self.op = self.make_operator()

    def cfg(self):
        from chat.settingsio import cfg
        return cfg()

    def test_nobody_is_available_without_a_heartbeat(self):
        self.assertFalse(presence.operator_available())

    def test_an_operator_who_switched_online_and_pings_is_available(self):
        presence.heartbeat(self.op, online=True)
        self.assertTrue(presence.operator_available())
        self.assertEqual(presence.online_count(), 1)

    def test_a_ping_alone_never_switches_the_operator_online(self):
        presence.heartbeat(self.op)
        self.assertFalse(presence.operator_available())
        presence.heartbeat(self.op, online=True)
        presence.heartbeat(self.op, online=False)
        presence.heartbeat(self.op)                                         # نبض خودکار صفحه
        self.assertFalse(presence.operator_available())

    def test_a_stale_heartbeat_makes_the_operator_unavailable(self):
        presence.heartbeat(self.op, online=True)
        timeout = self.cfg().chat_operator_timeout_seconds
        OperatorPresence.objects.update(last_seen=timezone.now() - timedelta(seconds=timeout + 5))
        cache.clear()
        self.assertFalse(presence.operator_available())
        presence.heartbeat(self.op)
        self.assertTrue(presence.operator_available())

    def test_going_offline_is_seen_immediately_despite_the_cache(self):
        presence.heartbeat(self.op, online=True)
        self.assertTrue(presence.operator_available())                      # کش ۵ ثانیه‌ای پر شد
        presence.heartbeat(self.op, online=False)
        self.assertFalse(presence.operator_available())

    def test_deactivated_or_demoted_operators_do_not_count(self):
        presence.heartbeat(self.op, online=True)
        self.op.is_active = False
        self.op.save()
        cache.clear()
        self.assertFalse(presence.operator_available())
        self.op.is_active, self.op.is_staff = True, False
        self.op.save()
        cache.clear()
        self.assertFalse(presence.operator_available())

    def test_availability_works_without_redis(self):
        presence.heartbeat(self.op, online=True)
        boom = mock.Mock(side_effect=OSError('down'))
        with mock.patch.multiple('chat.cache.cache', add=boom, incr=boom, get=boom, set=boom, delete=boom):
            self.assertTrue(presence.operator_available())

    def test_the_cache_makes_repeated_checks_cost_no_queries(self):
        presence.heartbeat(self.op, online=True)
        presence.operator_available()
        with self.assertNumQueries(0):
            presence.operator_available()


class LiveGateTests(ChatTestBase):
    """ سه وضعیت: ۱ زنده، ۲ ساعت کاری بدون کارشناس، ۳ خارج از ساعت """

    def setUp(self):
        super().setUp()
        self.op = self.make_operator()

    def cfg(self):
        from chat.settingsio import cfg
        return cfg()

    def state(self):
        return availability.live_state(self.cfg())

    def test_state_one_in_hours_with_an_online_operator(self):
        presence.heartbeat(self.op, online=True)
        self.assertEqual(self.state(), 'live')
        self.assertTrue(availability.live_available(self.cfg()))

    def test_state_two_in_hours_without_an_operator(self):
        self.assertEqual(self.state(), 'no_operator')
        self.assertFalse(availability.live_available(self.cfg()))

    def test_state_three_after_hours_even_with_an_operator_online(self):
        presence.heartbeat(self.op, online=True)
        self.set(chat_hours_mode='by_schedule', chat_hours_sat='', chat_hours_sun='', chat_hours_mon='', chat_hours_tue='',
                 chat_hours_wed='', chat_hours_thu='', chat_hours_fri='')
        self.assertEqual(self.state(), 'after_hours')
        self.assertFalse(availability.live_available(self.cfg()))

    def test_live_without_a_required_operator_is_allowed_in_hours(self):
        self.set(chat_live_requires_operator=False)
        self.assertEqual(self.state(), 'live')

    def test_a_disabled_live_tab_never_offers_live(self):
        presence.heartbeat(self.op, online=True)
        self.set(chat_tab_live_enabled=False)
        self.assertFalse(availability.live_available(self.cfg()))

    def test_the_config_endpoint_reports_each_state(self):
        client = Client()
        data = json.loads(client.get(reverse('chat:config')).content)
        self.assertEqual(data['availability'], {'live': False, 'state': 'no_operator'})
        presence.heartbeat(self.op, online=True)
        data = json.loads(client.get(reverse('chat:config')).content)
        self.assertEqual(data['availability'], {'live': True, 'state': 'live'})
        self.assertEqual(data['version'], 4)
        self.assertIn('typing', data['api'])
        for key in ('live_intro', 'live_waiting', 'typing'):
            self.assertTrue(data['texts'][key])
        self.assertTrue(data['typing_enabled'])


class LiveConversationTests(ApiBase):
    def setUp(self):
        super().setUp()
        self.op = self.make_operator()

    def go_live(self):
        presence.heartbeat(self.op, online=True)

    def live(self, client=None, **overrides):
        overrides.setdefault('channel', 'live')
        return self.create(client, **overrides)

    def test_a_live_conversation_starts_in_the_queue_with_an_sla_timer(self):
        self.go_live()
        client, response = self.live()
        self.assertEqual(response.status_code, 201)
        data = body(response)
        self.assertEqual((data['conversation']['status'], data['conversation']['channel']), (sm.WAITING_OPERATOR, 'live'))
        c = Conversation.objects.get()
        self.assertEqual((c.channel_origin, c.next_timer_kind, c.unread_for_operator), ('live', sm.TIMER_SLA, 1))
        self.assertEqual(list(c.events.values_list('type', flat=True)), ['T1'])
        sla = self.cfg().chat_operator_response_sla_minutes
        self.assertAlmostEqual((c.next_timer_at - timezone.now()).total_seconds(), sla * 60, delta=30)

    def cfg(self):
        from chat.settingsio import cfg
        return cfg()

    def test_live_is_refused_when_no_operator_is_online(self):
        _, response = self.live()
        self.assertEqual((response.status_code, body(response)['code']), (409, 'not_available'))
        self.assertEqual(Conversation.objects.count(), 0)

    def test_live_is_refused_when_the_live_tab_is_off(self):
        self.go_live()
        self.set(chat_tab_live_enabled=False)
        _, response = self.live()
        self.assertEqual((response.status_code, body(response)['code']), (403, 'disabled'))

    def test_an_unknown_channel_is_a_bad_request(self):
        _, response = self.live(channel='ai')
        self.assertEqual(body(response)['code'], 'bad_request')

    def test_the_operator_answering_a_live_conversation_goes_active_then_waits_for_the_customer(self):
        self.go_live()
        client, response = self.live()
        c = Conversation.objects.get()
        self.operator_says(c, self.op, 'سلام، بفرمایید')
        c = self.reload(c)
        self.assertEqual((c.status, c.next_timer_kind, c.assigned_operator_id), (sm.ACTIVE, sm.TIMER_CUSTOMER_IDLE, self.op.pk))

    def test_an_offline_conversation_goes_back_to_the_queue_when_an_operator_is_online(self):
        client, _ = self.create()                                           # آفلاین
        cid = body(client.get(reverse('chat:state')))['conversation']['id']
        self.go_live()
        response = post(client, reverse('chat:send', args=[cid]), {'body': 'کسی هست؟', 'client_msg_id': str(uuid.uuid4())})
        self.assertEqual(response.status_code, 201)
        c = Conversation.objects.get()
        self.assertEqual((c.status, c.next_timer_kind), (sm.WAITING_OPERATOR, sm.TIMER_SLA))                 # T11
        self.assertIsNone(c.assigned_operator_id)

    def test_operators_are_texted_for_a_live_conversation_only_when_nobody_is_online(self):
        self.set(chat_live_requires_operator=False)
        with mock.patch('chat.conversations.notify_operators_of_message') as notify, self.captureOnCommitCallbacks(execute=True):
            self.live()                                                     # بدون کارشناس آنلاین
        self.assertEqual(notify.call_count, 1)
        Conversation.objects.all().delete()
        self.go_live()
        with mock.patch('chat.conversations.notify_operators_of_message') as notify, self.captureOnCommitCallbacks(execute=True):
            self.live()                                                     # کارشناس آنلاین است ← پیامک لازم نیست
        self.assertEqual(notify.call_count, 0)

    def test_a_guest_can_chat_live_and_sees_the_state_availability(self):
        self.go_live()
        client, _ = self.live()
        state = body(client.get(reverse('chat:state')))
        self.assertEqual(state['availability'], {'live': True, 'state': 'live'})
        self.assertEqual(state['conversation']['status'], sm.WAITING_OPERATOR)


class ConsolePresenceTests(ConsoleBase):
    def test_the_operator_switches_online_and_back_through_the_console(self):
        data = jget(self.op_client, self.url('presence'))
        self.assertEqual((data['online'], data['online_count']), (False, 0))
        data = json.loads(jpost(self.op_client, self.url('presence'), {'online': True}).content)
        self.assertEqual((data['online'], data['online_count']), (True, 1))
        self.assertTrue(availability.operator_available())
        data = json.loads(jpost(self.op_client, self.url('presence'), {'online': False}).content)
        self.assertEqual((data['online'], data['online_count']), (False, 0))
        self.assertFalse(availability.operator_available())

    def test_a_bare_ping_only_refreshes_the_heartbeat(self):
        jpost(self.op_client, self.url('presence'), {'online': True})
        OperatorPresence.objects.update(last_seen=timezone.now() - timedelta(minutes=5))
        cache.clear()
        self.assertFalse(availability.operator_available())
        data = json.loads(jpost(self.op_client, self.url('presence'), {}).content)
        self.assertTrue(data['online'])                                     # کلید دستی دست‌نخورده
        self.assertTrue(availability.operator_available())

    def test_only_operators_can_use_presence(self):
        staff = self.make_user(is_staff=True)
        client = Client()
        client.force_login(staff)
        self.assertEqual(jpost(client, self.url('presence'), {'online': True}).status_code, 403)
        self.assertEqual(client.get(self.url('presence')).status_code, 403)
        self.assertEqual(Client().get(self.url('presence')).status_code, 302)
        self.assertEqual(OperatorPresence.objects.count(), 0)

    def test_the_console_page_exposes_the_presence_url(self):
        html = self.op_client.get(reverse('admin:chat_conversation_console')).content.decode()
        self.assertIn(self.url('presence'), html)
        self.assertIn(self.url('typing', 0), html)
        self.assertIn(reverse('admin:chat_quickreply_changelist'), html)            # پیوند «مدیریت» پاسخ‌های آماده


class TypingTests(ApiBase, ConsoleBase):
    def test_customer_typing_shows_for_the_operator_and_expires(self):
        client, response = self.create()
        c = Conversation.objects.get()
        cid = body(response)['conversation']['id']
        self.assertFalse(jget(self.op_client, self.url('detail', c.pk))['typing'])
        self.assertEqual(post(client, reverse('chat:typing', args=[cid])).status_code, 200)
        self.assertTrue(jget(self.op_client, self.url('detail', c.pk))['typing'])
        cache.clear()                                                       # انقضای کلید کوتاه‌عمر
        self.assertFalse(jget(self.op_client, self.url('detail', c.pk))['typing'])

    def test_operator_typing_shows_for_the_customer(self):
        client, response = self.create()
        c = Conversation.objects.get()
        cid = body(response)['conversation']['id']
        self.assertFalse(body(client.get(reverse('chat:messages', args=[cid])))['typing'])
        self.assertEqual(jpost(self.op_client, self.url('typing', c.pk)).status_code, 200)
        self.assertTrue(body(client.get(reverse('chat:messages', args=[cid])))['typing'])

    def test_typing_can_be_switched_off_in_settings(self):
        client, response = self.create()
        c = Conversation.objects.get()
        cid = body(response)['conversation']['id']
        self.set(chat_typing_indicator_enabled=False)
        post(client, reverse('chat:typing', args=[cid]))
        jpost(self.op_client, self.url('typing', c.pk))
        self.assertFalse(jget(self.op_client, self.url('detail', c.pk))['typing'])
        self.assertFalse(body(client.get(reverse('chat:messages', args=[cid])))['typing'])

    def test_typing_requires_ownership_like_every_customer_endpoint(self):
        _, response = self.create()
        cid = body(response)['conversation']['id']
        self.assertEqual(post(Client(), reverse('chat:typing', args=[cid])).status_code, 404)

    def test_closed_conversations_never_show_typing(self):
        client, response = self.create()
        c = Conversation.objects.get()
        cid = body(response)['conversation']['id']
        post(client, reverse('chat:typing', args=[cid]))
        conv.apply_rule(c, 'T17', sm.OPERATOR, operator=self.op)
        self.assertFalse(jget(self.op_client, self.url('detail', c.pk))['typing'])

    def test_typing_without_redis_is_silently_ignored(self):
        client, response = self.create()
        c = Conversation.objects.get()
        cid = body(response)['conversation']['id']
        boom = mock.Mock(side_effect=OSError('down'))
        with mock.patch.multiple('chat.cache.cache', add=boom, incr=boom, get=boom, set=boom):
            self.assertEqual(post(client, reverse('chat:typing', args=[cid])).status_code, 200)
            self.assertFalse(body(client.get(reverse('chat:messages', args=[cid])))['typing'])

    def test_typing_is_rate_limited_and_never_touches_the_database(self):
        client, response = self.create()
        cid = body(response)['conversation']['id']
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as queries:
            post(client, reverse('chat:typing', args=[cid]))
        writes = [q['sql'] for q in queries if q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))]
        self.assertEqual(writes, [])                                        # نشانگر فقط Redis است
        before = Conversation.objects.values_list('last_activity_at', flat=True).get()
        for _ in range(50):
            post(client, reverse('chat:typing', args=[cid]))
        self.assertEqual(Conversation.objects.values_list('last_activity_at', flat=True).get(), before)
