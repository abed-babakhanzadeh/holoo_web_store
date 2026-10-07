"""
API عمومی گفتگو: ساخت پیام آفلاین، پولینگ، ارسال، خواندن، بستن؛ ماتریس IDOR؛ محدودیت نرخ؛ قطع Redis؛ اتصال مهمان به حساب.
"""
import json
import uuid
from datetime import timedelta
from unittest import mock

from django.contrib.auth.signals import user_logged_in
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils import timezone

from chat import cache as chatcache
from chat import conversations as conv
from chat import identity
from chat import statemachine as sm
from chat.models import ChatEvent, ChatMessage, Conversation
from chat.testing import ChatTestBase


def post(client, url, data=None, **extra):
    return client.post(url, data=json.dumps(data or {}), content_type='application/json', **extra)


def body(response):
    return json.loads(response.content)


class ApiBase(ChatTestBase):
    def create(self, client=None, **overrides):
        client = client or Client()
        data = {'channel': 'offline', 'message': 'سلام، یک سؤال دارم', 'client_msg_id': str(uuid.uuid4()), 'name': 'علی', 'phone': '09123334444',
                'page_path': '/shop/?utm_source=x#top', 'elapsed_ms': 5000, 'website': ''}
        data.update(overrides)
        return client, post(client, reverse('chat:create'), data)


class CreateTests(ApiBase):
    def test_a_guest_starts_an_offline_conversation_and_gets_the_visitor_cookie(self):
        client, response = self.create()
        self.assertEqual(response.status_code, 201)
        data = body(response)
        self.assertTrue(data['ok'])
        self.assertEqual(data['conversation']['status'], sm.OFFLINE)
        self.assertEqual(data['message']['seq'], 1)
        cookie = response.cookies['chat_vid']
        self.assertTrue(cookie['httponly'])
        self.assertEqual(cookie['samesite'], 'Lax')
        c = Conversation.objects.get()
        self.assertEqual((c.guest_name, c.guest_phone, c.source_path), ('علی', '09123334444', '/shop/'))     # فقط مسیر پایه
        self.assertEqual(c.visitor_hash, identity.hash_token(cookie.value))
        self.assertNotIn(cookie.value, c.visitor_hash)                                                       # فقط هش ذخیره می‌شود
        self.assertEqual(c.client_ip_trunc, '127.0.0.0')

    def test_a_second_message_continues_the_same_conversation(self):
        client, first = self.create()
        _, second = self.create(client, message='پیام دوم')
        self.assertEqual(Conversation.objects.count(), 1)
        self.assertEqual(body(second)['message']['seq'], 2)

    def test_resending_the_same_client_id_is_idempotent(self):
        key = str(uuid.uuid4())
        client, _ = self.create(client_msg_id=key)
        _, again = self.create(client, client_msg_id=key)
        self.assertEqual(again.status_code, 201)
        self.assertEqual(ChatMessage.objects.count(), 1)

    def test_a_logged_in_user_is_known_without_asking_and_supplied_fields_are_ignored(self):
        user = self.make_user(first_name='رضا', last_name='احمدی')
        client = Client()
        client.force_login(user)
        _, response = self.create(client, name='جعلی', phone='09120000000', elapsed_ms=0)
        self.assertEqual(response.status_code, 201)
        c = Conversation.objects.get()
        self.assertEqual((c.user_id, c.guest_name, c.guest_phone), (user.pk, '', ''))
        self.assertEqual(c.display_name, 'رضا احمدی')

    def test_validation_errors_use_the_fixed_error_envelope(self):
        cases = {
            'empty': dict(message='   '), 'too_long': dict(message='x' * 1001), 'bad_phone': dict(phone='0912'),
            'spam': dict(website='http://spam.example'), 'too_fast': dict(elapsed_ms=300), 'bad_request': dict(client_msg_id='نه'),
            'not_available': dict(channel='live'),
        }
        for code, overrides in cases.items():
            with self.subTest(code=code):
                _, response = self.create(**overrides)
                data = body(response)
                self.assertEqual(response.status_code, 400 if code != 'not_available' else 409)
                self.assertEqual((data['ok'], data['code']), (False, code))
                self.assertTrue(data['message'])
        self.assertEqual(Conversation.objects.count(), 0)

    def test_guest_field_modes(self):
        self.set(chat_enabled=True, chat_guest_name_mode='required', chat_guest_phone_mode='hidden')
        _, response = self.create(name='', phone='09123334444')
        self.assertEqual(body(response)['code'], 'field_required')
        _, ok_response = self.create(name='سارا', phone='09123334444')
        self.assertEqual(ok_response.status_code, 201)
        c = Conversation.objects.get()
        self.assertEqual((c.guest_name, c.guest_phone), ('سارا', ''))                                        # مخفی ← ذخیره نمی‌شود

    def test_persian_digit_phones_are_normalized(self):
        _, response = self.create(phone='۰۹۱۲۳۳۳۴۴۴۴')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Conversation.objects.get().guest_phone, '09123334444')

    def test_a_logged_in_user_skips_the_form_speed_check(self):
        client = Client()
        client.force_login(self.make_user())
        _, response = self.create(client, elapsed_ms=0)
        self.assertEqual(response.status_code, 201)

    def test_disabled_states(self):
        self.set(chat_enabled=False)
        self.assertEqual(self.create()[1].status_code, 403)
        self.set(chat_enabled=True, chat_tab_offline_enabled=False)
        self.assertEqual(self.create()[1].status_code, 403)
        self.set(chat_enabled=True, chat_tab_offline_enabled=True, chat_visible_for_guests=False)
        self.assertEqual(self.create()[1].status_code, 403)
        self.assertEqual(Conversation.objects.count(), 0)

    def test_only_post_is_allowed(self):
        self.assertEqual(Client().get(reverse('chat:create')).status_code, 405)

    def test_csrf_is_enforced(self):
        strict = Client(enforce_csrf_checks=True)
        response = post(strict, reverse('chat:create'), {'message': 'x'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Conversation.objects.count(), 0)

    def test_the_page_path_is_reduced_to_the_base_path(self):
        for raw, expected in (('/a/b/?utm=1', '/a/b/'), ('/x#frag', '/x'), ('javascript:alert(1)', ''), ('', ''), ('//evil.example/x', '//evil.example/x')):
            with self.subTest(raw=raw):
                Conversation.objects.all().delete()
                self.create(page_path=raw)
                got = Conversation.objects.get().source_path
                self.assertEqual(got, expected)


class RateLimitTests(ApiBase):
    def test_messages_per_minute_are_capped(self):
        self.set(chat_enabled=True, chat_rate_limit_per_minute=2)
        client, first = self.create()
        self.assertEqual(first.status_code, 201)
        self.assertEqual(self.create(client)[1].status_code, 201)
        limited = self.create(client)[1]
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(body(limited)['code'], 'rate_limited')

    def test_new_conversations_per_day_are_capped_per_ip_but_continuing_one_is_free(self):
        self.set(chat_enabled=True, chat_guest_max_conversations_per_day=1, chat_rate_limit_per_minute=60)
        client, first = self.create()
        self.assertEqual(first.status_code, 201)
        self.assertEqual(self.create(client)[1].status_code, 201)                                  # ادامه‌ی همان گفتگو شمرده نمی‌شود
        other, second = self.create(Client())
        self.assertEqual(second.status_code, 429)
        self.assertEqual(body(second)['code'], 'daily_cap')


class MessagesApiTests(ApiBase):
    def setUp(self):
        super().setUp()
        self.client_a, response = self.create()
        self.cid = body(response)['conversation']['id']
        self.operator = self.make_operator()
        c = Conversation.objects.get()
        self.operator_says(c, self.operator, 'سلام، بفرمایید')
        conv.post_message(self.reload(c), sender='operator', body='یادداشت داخلی', operator=self.operator, internal=True)

    def url(self, name):
        return reverse(f'chat:{name}', args=[self.cid])

    def test_the_customer_sees_replies_but_never_internal_notes(self):
        data = body(self.client_a.get(self.url('messages') + '?after=0'))
        self.assertEqual([m['body'] for m in data['messages']], ['سلام، یک سؤال دارم', 'سلام، بفرمایید'])
        self.assertEqual(data['messages'][1]['operator'], 'کارشناس')
        self.assertEqual(data['conversation']['unread'], 1)
        self.assertNotIn('note', data['messages'][0])
        self.assertNotIn('یادداشت داخلی', json.dumps(data, ensure_ascii=False))

    def test_the_cursor_returns_only_newer_messages_and_costs_two_queries_at_most(self):
        with self.assertNumQueries(2):                      # دسترسی + پیام‌ها
            data = body(self.client_a.get(self.url('messages') + '?after=1'))
        self.assertEqual([m['seq'] for m in data['messages']], [2])
        with self.assertNumQueries(1):                      # دسترسی؛ چیز تازه‌ای نیست
            self.assertEqual(body(self.client_a.get(self.url('messages') + '?after=3'))['messages'], [])

    def test_send_is_idempotent_and_updates_the_conversation(self):
        key = str(uuid.uuid4())
        first = post(self.client_a, self.url('send'), {'body': 'پیام تازه', 'client_msg_id': key})
        again = post(self.client_a, self.url('send'), {'body': 'پیام تازه', 'client_msg_id': key})
        self.assertEqual((first.status_code, again.status_code), (201, 200))
        self.assertEqual(body(again)['created'], False)
        self.assertEqual(ChatMessage.objects.filter(client_msg_id=key).count(), 1)
        self.assertEqual(Conversation.objects.get().status, sm.ACTIVE)                               # T7: مسئول داشت

    def test_read_moves_the_customer_watermark_and_clears_unread(self):
        data = body(post(self.client_a, self.url('read'), {'upto_seq': 2}))
        self.assertEqual((data['conversation']['unread'], data['conversation']['last_read_seq']), (0, 2))

    def test_customer_can_close_and_the_conversation_disappears_from_state(self):
        self.assertTrue(body(self.client_a.get(reverse('chat:state')))['conversation'])
        data = body(post(self.client_a, self.url('close')))
        self.assertTrue(data['conversation']['closed'])
        self.assertIsNone(body(self.client_a.get(reverse('chat:state')))['conversation'])
        self.assertEqual(post(self.client_a, self.url('close')).status_code, 409)

    def test_state_reports_availability_and_marks_the_customer_as_seen(self):
        data = body(self.client_a.get(reverse('chat:state')))
        self.assertEqual(data['availability']['live'], False)
        self.assertEqual(data['conversation']['id'], self.cid)
        self.assertTrue(chatcache.customer_seen_recently(Conversation.objects.get().pk))

    def test_state_for_a_stranger_has_no_conversation(self):
        self.assertIsNone(body(Client().get(reverse('chat:state')))['conversation'])

    def test_state_when_chat_is_off(self):
        self.set(chat_enabled=False)
        self.assertEqual(body(self.client_a.get(reverse('chat:state'))), {'ok': True, 'enabled': False})

    def test_a_garbage_cursor_is_harmless(self):
        self.assertEqual(self.client_a.get(self.url('messages') + '?after=zzz').status_code, 200)


class IdorMatrixTests(ApiBase):
    """ هر endpoint عمومی برای هر غیرمالکی دقیقاً همان ۴۰۴ «نیست» را می‌دهد """

    def setUp(self):
        super().setUp()
        self.owner_client, response = self.create()
        self.guest_cid = body(response)['conversation']['id']
        self.owner_user = self.make_user()
        self.user_client = Client()
        self.user_client.force_login(self.owner_user)
        _, user_response = self.create(self.user_client)
        self.user_cid = body(user_response)['conversation']['id']
        self.other_guest = Client()
        self.create(self.other_guest, message='مهمان دیگر')                         # کوکی خودش را دارد
        self.other_user = Client()
        self.other_user.force_login(self.make_user())
        self.stranger = Client()

    def endpoints(self, cid):
        return [('get', reverse('chat:messages', args=[cid])), ('post', reverse('chat:send', args=[cid])),
                ('post', reverse('chat:read', args=[cid])), ('post', reverse('chat:close', args=[cid]))]

    def call(self, client, method, url):
        if method == 'get':
            return client.get(url)
        return post(client, url, {'body': 'نفوذ', 'client_msg_id': str(uuid.uuid4()), 'upto_seq': 1})

    def test_owners_can_use_every_endpoint(self):
        for client, cid in ((self.owner_client, self.guest_cid), (self.user_client, self.user_cid)):
            for method, url in self.endpoints(cid)[:3]:
                with self.subTest(url=url):
                    self.assertIn(self.call(client, method, url).status_code, (200, 201))

    def test_everyone_else_gets_the_same_404_as_for_an_unknown_id(self):
        unknown = str(uuid.uuid4())
        for cid in (self.guest_cid, self.user_cid):
            for who, client in (('stranger', self.stranger), ('other_guest', self.other_guest), ('other_user', self.other_user)):
                for method, url in self.endpoints(cid):
                    with self.subTest(who=who, url=url):
                        response = self.call(client, method, url)
                        self.assertEqual(response.status_code, 404)
                        reference = self.call(client, method, url.replace(cid, unknown))
                        self.assertEqual(response.content, reference.content)                    # فرقی بین «نیست» و «مال دیگری» نیست
        self.assertEqual(ChatMessage.objects.filter(body='نفوذ').count(), 0)
        self.assertEqual(Conversation.objects.exclude(status=sm.OFFLINE).count(), 0)

    def test_a_user_cannot_use_a_guest_conversation_and_a_guest_cookie_cannot_open_a_user_conversation_of_someone_else(self):
        self.assertEqual(self.call(self.user_client, 'get', reverse('chat:messages', args=[self.guest_cid])).status_code, 404)
        self.assertEqual(self.call(self.owner_client, 'get', reverse('chat:messages', args=[self.user_cid])).status_code, 404)

    def test_a_forged_cookie_value_does_not_help(self):
        forged = Client()
        forged.cookies['chat_vid'] = 'x' * 43
        self.assertEqual(self.call(forged, 'get', reverse('chat:messages', args=[self.guest_cid])).status_code, 404)
        empty = Client()
        empty.cookies['chat_vid'] = ''
        self.assertEqual(self.call(empty, 'get', reverse('chat:messages', args=[self.guest_cid])).status_code, 404)

    def test_the_phone_number_is_never_proof_of_ownership(self):
        # کاربر واردشده‌ای با همان شماره‌ی مهمان هم مالک نمی‌شود
        Conversation.objects.filter(public_id=self.guest_cid).update(guest_phone=self.owner_user.phone_number)
        self.assertEqual(self.call(self.other_user, 'get', reverse('chat:messages', args=[self.guest_cid])).status_code, 404)

    def test_malformed_ids_are_404_not_500(self):
        response = Client().get('/chat/c/not-a-uuid/messages/')
        self.assertEqual(response.status_code, 404)


class GuestAttachTests(ApiBase):
    def attach(self, user, token):
        request = RequestFactory().get('/')
        request.COOKIES['chat_vid'] = token
        user_logged_in.send(sender=user.__class__, request=request, user=user)

    def make_guest_conversation(self, **updates):
        client, response = self.create()
        token = client.cookies['chat_vid'].value
        c = Conversation.objects.get(public_id=body(response)['conversation']['id'])
        if updates:
            Conversation.objects.filter(pk=c.pk).update(**updates)
        return token, c

    def test_logging_in_attaches_the_conversations_of_this_browser(self):
        token, c = self.make_guest_conversation()
        user = self.make_user()
        self.attach(user, token)
        c.refresh_from_db()
        self.assertEqual(c.user_id, user.pk)
        event = ChatEvent.objects.get(conversation=c, type='attached')
        self.assertTrue(event.meta['phone_mismatch'])                                              # شماره‌ی مهمان با حساب فرق داشت: فقط پرچم
        # حالا کاربر بدون کوکی هم به گفتگو دسترسی دارد
        client = Client()
        client.force_login(user)
        self.assertEqual(client.get(reverse('chat:messages', args=[c.public_id])).status_code, 200)

    def test_same_phone_means_no_mismatch_flag(self):
        token, c = self.make_guest_conversation()
        user = self.make_user()
        Conversation.objects.filter(pk=c.pk).update(guest_phone=user.phone_number)
        self.attach(user, token)
        self.assertFalse(ChatEvent.objects.get(conversation=c, type='attached').meta['phone_mismatch'])

    def test_nothing_else_is_attached(self):
        token, mine = self.make_guest_conversation()
        _, theirs = self.make_guest_conversation(visitor_hash=identity.hash_token('someone-else-' + 'x' * 30))
        owned = self.make_user()
        _, taken = self.make_guest_conversation(user=owned)
        _, old = self.make_guest_conversation(created_at=timezone.now() - timedelta(days=40))
        # old/taken/theirs هم‌کوکی نیستند یا مالک دارند یا قدیمی‌اند؛ فقط mine
        Conversation.objects.exclude(pk=mine.pk).update(visitor_hash=identity.hash_token('other-' + 'y' * 30))
        Conversation.objects.filter(pk=old.pk).update(visitor_hash=identity.hash_token(token))
        Conversation.objects.filter(pk=taken.pk).update(visitor_hash=identity.hash_token(token))
        user = self.make_user()
        self.attach(user, token)
        self.assertEqual(Conversation.objects.get(pk=mine.pk).user_id, user.pk)
        self.assertEqual(Conversation.objects.get(pk=taken.pk).user_id, owned.pk)                 # مالک‌دار دست نمی‌خورد
        self.assertIsNone(Conversation.objects.get(pk=old.pk).user_id)                            # قدیمی‌تر از ۳۰ روز
        self.assertIsNone(Conversation.objects.get(pk=theirs.pk).user_id)

    def test_login_without_the_visitor_cookie_does_nothing_and_never_breaks_login(self):
        _, c = self.make_guest_conversation()
        user = self.make_user()
        request = RequestFactory().get('/')
        user_logged_in.send(sender=user.__class__, request=request, user=user)
        user_logged_in.send(sender=user.__class__, request=None, user=user)
        c.refresh_from_db()
        self.assertIsNone(c.user_id)


class RedisOutageTests(ApiBase):
    """ قطع Redis چت را نمی‌شکند؛ محدودیت‌ها با دیتابیس ادامه می‌دهند """

    def outage(self):
        boom = mock.Mock(side_effect=OSError('redis down'))
        return mock.patch.multiple('chat.cache.cache', add=boom, incr=boom, get=boom, set=boom)

    def test_the_whole_conversation_flow_works_without_redis(self):
        with self.outage():
            client, response = self.create()
            self.assertEqual(response.status_code, 201)
            cid = body(response)['conversation']['id']
            self.assertEqual(client.get(reverse('chat:messages', args=[cid])).status_code, 200)
            self.assertEqual(post(client, reverse('chat:send', args=[cid]), {'body': 'x', 'client_msg_id': str(uuid.uuid4())}).status_code, 201)
            self.assertEqual(client.get(reverse('chat:state')).status_code, 200)
            self.assertFalse(chatcache.available())                                                 # مدارشکن باز شد

    def test_the_message_rate_limit_falls_back_to_the_database(self):
        self.set(chat_enabled=True, chat_rate_limit_per_minute=2)
        with self.outage():
            client, first = self.create()
            self.assertEqual(first.status_code, 201)
            self.assertEqual(self.create(client)[1].status_code, 201)
            self.assertEqual(self.create(client)[1].status_code, 429)

    def test_the_daily_cap_falls_back_to_the_database(self):
        self.set(chat_enabled=True, chat_guest_max_conversations_per_day=1, chat_rate_limit_per_minute=60)
        with self.outage():
            self.assertEqual(self.create()[1].status_code, 201)
            self.assertEqual(self.create(Client())[1].status_code, 429)

    def test_the_breaker_stops_hammering_redis(self):
        calls = mock.Mock(side_effect=OSError('down'))
        with mock.patch('chat.cache.cache.add', calls):
            for _ in range(5):
                chatcache.acquire('k', 10)
        self.assertEqual(calls.call_count, 1)                                                        # بعد از اولین خطا ۳۰ ثانیه سراغش نمی‌رود

    def test_the_breaker_closes_again(self):
        with self.outage():
            chatcache.acquire('k', 10)
        self.assertFalse(chatcache.available())
        chatcache.reset_breaker()
        self.assertTrue(chatcache.available())
        self.assertTrue(chatcache.acquire('fresh', 10))
