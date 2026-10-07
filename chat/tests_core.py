"""
هسته‌ی گفتگو: ماتریس ماشین وضعیت (مجاز و ممنوع)، جریان‌های هر انتقال، تایمرها، idempotency، seq، واترمارک خوانده‌شدن.
"""
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.test import SimpleTestCase

from chat import conversations as conv
from chat import statemachine as sm
from chat.models import ChatEvent, ChatMessage, Conversation
from chat.testing import ChatTestBase

NOW = datetime(2026, 10, 10, 9, 0, tzinfo=dt_timezone.utc)


class Cfg:
    """ نمونه‌ی ساده‌ی تنظیمات برای تایمرها """
    chat_operator_response_sla_minutes = 10
    chat_customer_idle_minutes = 10
    chat_customer_gone_minutes = 30
    chat_idle_close_hours = 48


class StateMachineMatrixTests(SimpleTestCase):
    def test_every_combination_is_allowed_exactly_when_the_matrix_says_so(self):
        for rule in sm.RULES.values():
            for source in list(sm.ALL_STATES) + [sm.NEW]:
                for actor in sm.ACTORS:
                    with self.subTest(rule=rule.id, source=source, actor=actor):
                        if source in rule.sources and actor in rule.actors:
                            self.assertEqual(sm.check(rule.id, source, actor), rule)
                        else:
                            with self.assertRaises(sm.InvalidTransition):
                                sm.check(rule.id, source, actor)

    def test_unknown_rule_is_rejected(self):
        with self.assertRaises(sm.InvalidTransition):
            sm.check('T99', sm.OFFLINE, sm.OPERATOR)

    def test_no_rule_ever_produces_a_forbidden_pair(self):
        produced = {(source, rule.target) for rule in sm.RULES.values() for source in rule.sources}
        self.assertEqual(produced & sm.FORBIDDEN_PAIRS, set())

    def test_a_closed_conversation_can_only_be_reopened_into_the_queue(self):
        self.assertEqual(sm.allowed_rules(sm.CLOSED, sm.OPERATOR), ['T18'])
        self.assertEqual(sm.allowed_rules(sm.CLOSED, sm.CUSTOMER), ['T18'])
        self.assertEqual(sm.allowed_rules(sm.CLOSED, sm.SYSTEM), [])
        self.assertEqual(sm.RULES['T18'].target, sm.WAITING_OPERATOR)

    def test_the_system_may_enter_the_queue_only_through_t15(self):
        for rule in sm.RULES.values():
            if rule.target == sm.WAITING_OPERATOR and sm.SYSTEM in rule.actors:
                self.assertEqual(rule.id, 'T15')

    def test_only_customers_create_conversations(self):
        for rule in sm.RULES.values():
            if sm.NEW in rule.sources:
                self.assertEqual(rule.actors, frozenset({sm.CUSTOMER}))

    def test_no_rule_mentions_presence(self):
        # حضور کارشناس هرگز انتقال نمی‌سازد: هیچ عاملی غیر از مشتری/کارشناس/سیستم نیست و قاعده‌ای با نام presence وجود ندارد
        self.assertEqual(set(sm.ACTORS), {'customer', 'operator', 'system'})
        self.assertFalse([r for r in sm.RULES.values() if 'presence' in r.title.lower()])

    def test_every_open_state_can_be_closed_by_every_actor(self):
        self.assertEqual(sm.RULES['T17'].sources, sm.OPEN_STATES)
        self.assertEqual(sm.RULES['T17'].actors, frozenset(sm.ACTORS))

    def test_actions_offered_to_operators_per_state(self):
        self.assertEqual(sm.allowed_rules(sm.WAITING_OPERATOR, sm.OPERATOR), ['T3', 'T17'])
        self.assertIn('T6', sm.allowed_rules(sm.ACTIVE, sm.OPERATOR))
        self.assertIn('T16', sm.allowed_rules(sm.ACTIVE, sm.OPERATOR))
        self.assertIn('T13', sm.allowed_rules(sm.OFFLINE, sm.OPERATOR))


class TimerTests(SimpleTestCase):
    def timer(self, rule, cfg=None, kind='', at=None):
        return sm.timer_for(rule, NOW, cfg or Cfg(), kind, at)

    def test_each_rule_sets_the_expected_timer(self):
        minutes = lambda n: NOW + timedelta(minutes=n)
        expected = {
            'T1': (minutes(10), sm.TIMER_SLA), 'T7': (minutes(10), sm.TIMER_SLA), 'T7b': (minutes(10), sm.TIMER_SLA),
            'T11': (minutes(10), sm.TIMER_SLA), 'T15': (minutes(10), sm.TIMER_SLA), 'T18': (minutes(10), sm.TIMER_SLA),
            'T3': (minutes(10), sm.TIMER_CUSTOMER_IDLE), 'T4': (minutes(10), sm.TIMER_CUSTOMER_IDLE),
            'T6': (minutes(30), sm.TIMER_CUSTOMER_GONE), 'T14': (minutes(30), sm.TIMER_CUSTOMER_GONE),
            'T2': (NOW + timedelta(hours=48), sm.TIMER_IDLE_CLOSE), 'T12': (NOW + timedelta(hours=48), sm.TIMER_IDLE_CLOSE),
            'T13': (NOW + timedelta(hours=48), sm.TIMER_IDLE_CLOSE), 'T8': (NOW + timedelta(hours=48), sm.TIMER_IDLE_CLOSE),
            'T9': (NOW + timedelta(hours=48), sm.TIMER_IDLE_CLOSE), 'T10': (NOW + timedelta(hours=48), sm.TIMER_IDLE_CLOSE),
            'T17': (None, ''),
        }
        for rule, value in expected.items():
            with self.subTest(rule=rule):
                self.assertEqual(self.timer(rule), value)

    def test_a_running_sla_is_never_extended_by_more_customer_messages(self):
        started = NOW - timedelta(minutes=4)
        deadline = started + timedelta(minutes=10)
        for rule in ('T5', 'T20'):
            self.assertEqual(self.timer(rule, kind=sm.TIMER_SLA, at=deadline), (deadline, sm.TIMER_SLA))
        self.assertEqual(self.timer('T5', kind=sm.TIMER_CUSTOMER_IDLE, at=deadline), (NOW + timedelta(minutes=10), sm.TIMER_SLA))

    def test_sla_zero_disables_the_sla_timer(self):
        cfg = Cfg()
        cfg.chat_operator_response_sla_minutes = 0
        self.assertEqual(self.timer('T1', cfg), (None, ''))
        self.assertEqual(self.timer('T5', cfg), (None, ''))

    def test_reassignment_keeps_the_timer(self):
        at = NOW + timedelta(minutes=3)
        self.assertEqual(self.timer('T16', kind=sm.TIMER_SLA, at=at), (at, sm.TIMER_SLA))


class ConversationFlowTests(ChatTestBase):
    def setUp(self):
        super().setUp()
        self.op = self.make_operator()
        self.op2 = self.make_operator()

    def events(self, c):
        return list(ChatEvent.objects.filter(conversation=c).order_by('id').values_list('type', 'from_status', 'to_status'))

    # ---- T2 / T12 / T13 / T14 / T7 / T4 / T5
    def test_t2_creates_an_async_conversation_with_the_first_message(self):
        c = self.new_conversation(body='سلام، قیمت؟')
        self.assertEqual((c.status, c.channel_origin, c.last_message_seq, c.unread_for_operator), (sm.OFFLINE, 'offline', 1, 1))
        self.assertEqual(c.next_timer_kind, sm.TIMER_IDLE_CLOSE)
        self.assertEqual(c.last_message_preview, 'سلام، قیمت؟')
        self.assertEqual(self.events(c), [('T2', '', sm.OFFLINE)])
        self.assertEqual(ChatMessage.objects.get(conversation=c).seq, 1)

    def test_a_whole_async_conversation_walks_the_matrix(self):
        c = self.new_conversation()
        self.customer_says(c)                                           # T12
        c = self.reload(c)
        self.assertEqual((c.status, c.unread_for_operator, c.last_message_seq), (sm.OFFLINE, 2, 2))

        self.operator_says(c, self.op)                                  # T13
        c = self.reload(c)
        self.assertEqual((c.status, c.assigned_operator_id, c.unread_for_customer), (sm.WAITING_CUSTOMER, self.op.pk, 1))
        self.assertEqual(c.next_timer_kind, sm.TIMER_IDLE_CLOSE)

        self.operator_says(c, self.op)                                  # T14
        c = self.reload(c)
        self.assertEqual((c.status, c.unread_for_customer, c.next_timer_kind), (sm.WAITING_CUSTOMER, 2, sm.TIMER_CUSTOMER_GONE))

        self.customer_says(c)                                           # T7 (مسئول دارد)
        c = self.reload(c)
        self.assertEqual((c.status, c.assigned_operator_id, c.next_timer_kind), (sm.ACTIVE, self.op.pk, sm.TIMER_SLA))

        self.operator_says(c, self.op)                                  # T4
        c = self.reload(c)
        self.assertEqual((c.status, c.next_timer_kind), (sm.ACTIVE, sm.TIMER_CUSTOMER_IDLE))

        self.customer_says(c)                                           # T5
        first = self.reload(c)
        self.assertEqual((first.status, first.next_timer_kind), (sm.ACTIVE, sm.TIMER_SLA))
        self.customer_says(c, 'دوباره')                                 # T5: SLA قبلی تمدید نمی‌شود
        second = self.reload(c)
        self.assertEqual(second.next_timer_at, first.next_timer_at)
        self.assertEqual(second.unread_for_operator, first.unread_for_operator + 1)

        types = [e[0] for e in self.events(c)]
        self.assertEqual(types, ['T2', 'T12', 'T13', 'T14', 'T7', 'T4', 'T5', 'T5'])
        self.assertEqual(list(ChatMessage.objects.filter(conversation=c).values_list('seq', flat=True)), list(range(1, 9)))

    def test_t6_waiting_customer_by_operator_then_t7_brings_the_customer_back(self):
        c = self.new_conversation(status=sm.ACTIVE, assigned_operator=self.op)
        c = conv.apply_rule(c, 'T6', sm.OPERATOR, operator=self.op)
        self.assertEqual((c.status, c.next_timer_kind), (sm.WAITING_CUSTOMER, sm.TIMER_CUSTOMER_GONE))
        self.customer_says(c)
        self.assertEqual(self.reload(c).status, sm.ACTIVE)

    def test_t7b_returning_customer_without_an_assignee_goes_back_to_the_queue(self):
        c = self.new_conversation(status=sm.WAITING_CUSTOMER)
        self.customer_says(c)
        c = self.reload(c)
        self.assertEqual((c.status, c.next_timer_kind), (sm.WAITING_OPERATOR, sm.TIMER_SLA))

    # ---- صف: T20 / T3 / T15 / T16
    def test_queue_flow_t20_t3_t16_t15(self):
        c = self.new_conversation(status=sm.WAITING_OPERATOR)
        self.customer_says(c)                                           # T20
        c = self.reload(c)
        self.assertEqual((c.status, c.unread_for_operator), (sm.WAITING_OPERATOR, 2))
        self.operator_says(c, self.op)                                  # T3: اولین پاسخ ← برداشتن
        c = self.reload(c)
        self.assertEqual((c.status, c.assigned_operator_id), (sm.ACTIVE, self.op.pk))
        c = conv.apply_rule(c, 'T16', sm.OPERATOR, operator=self.op, new_operator=self.op2)
        self.assertEqual(c.assigned_operator_id, self.op2.pk)
        c = conv.apply_rule(c, 'T15', sm.OPERATOR, operator=self.op2)
        self.assertEqual((c.status, c.assigned_operator_id, c.next_timer_kind), (sm.WAITING_OPERATOR, None, sm.TIMER_SLA))
        self.assertEqual([e[0] for e in self.events(c)], ['T2', 'T20', 'T3', 'T16', 'T15'])

    def test_t3_can_also_be_a_claim_without_a_message(self):
        c = self.new_conversation(status=sm.WAITING_OPERATOR)
        c = conv.apply_rule(c, 'T3', sm.OPERATOR, operator=self.op)
        self.assertEqual((c.status, c.assigned_operator_id, c.next_timer_kind), (sm.ACTIVE, self.op.pk, sm.TIMER_CUSTOMER_IDLE))

    def test_system_transitions_t8_t9_t10_and_t15(self):
        for rule, source in (('T8', sm.WAITING_CUSTOMER), ('T9', sm.WAITING_OPERATOR), ('T10', sm.ACTIVE)):
            with self.subTest(rule=rule):
                c = self.new_conversation(status=source, assigned_operator=self.op if source == sm.ACTIVE else None)
                c = conv.apply_rule(c, rule, sm.SYSTEM)
                self.assertEqual((c.status, c.next_timer_kind), (sm.OFFLINE, sm.TIMER_IDLE_CLOSE))
                if source == sm.ACTIVE:
                    self.assertEqual(c.assigned_operator_id, self.op.pk)                  # T10: مسئول حفظ می‌شود
        c = self.new_conversation(status=sm.ACTIVE, assigned_operator=self.op)
        c = conv.apply_rule(c, 'T15', sm.SYSTEM)
        self.assertEqual((c.status, c.assigned_operator_id), (sm.WAITING_OPERATOR, None))

    # ---- T11 در برابر T12
    def test_t11_requires_working_hours_and_an_available_operator_otherwise_t12(self):
        c = self.new_conversation()
        with mock.patch('chat.conversations.live_available', return_value=True):
            self.customer_says(c, 'کسی هست؟')
        c = self.reload(c)
        self.assertEqual((c.status, c.assigned_operator_id, c.next_timer_kind), (sm.WAITING_OPERATOR, None, sm.TIMER_SLA))
        c2 = self.new_conversation(visitor='w' * 40)
        with mock.patch('chat.conversations.live_available', return_value=False):
            self.customer_says(c2)
        self.assertEqual(self.reload(c2).status, sm.OFFLINE)

    def test_presence_never_changes_a_running_conversation(self):
        c = self.new_conversation(status=sm.ACTIVE, assigned_operator=self.op)
        for available in (True, False, True):
            with mock.patch('chat.conversations.live_available', return_value=available):
                self.customer_says(c)
            self.assertEqual(self.reload(c).status, sm.ACTIVE)

    # ---- T17 / T18
    def test_close_and_reopen_by_customer_inside_the_window(self):
        c = self.new_conversation(status=sm.ACTIVE, assigned_operator=self.op, unread_for_operator=3)
        c = conv.apply_rule(c, 'T17', sm.OPERATOR, operator=self.op)
        self.assertEqual((c.status, c.unread_for_operator, c.next_timer_at), (sm.CLOSED, 0, None))
        self.assertIsNotNone(c.closed_at)
        self.customer_says(c, 'یادم رفت بپرسم')                          # T18
        c = self.reload(c)
        self.assertEqual((c.status, c.assigned_operator_id, c.closed_at, c.unread_for_operator, c.next_timer_kind),
                         (sm.WAITING_OPERATOR, None, None, 1, sm.TIMER_SLA))
        self.assertEqual(self.events(c)[-1][0], 'T18')

    def test_reopening_is_refused_after_the_window_or_when_switched_off(self):
        c = self.new_conversation(status=sm.CLOSED, closed_at=conv.timezone.now() - timedelta(hours=100))
        with self.assertRaises(conv.ChatError) as caught:
            self.customer_says(c)
        self.assertEqual(caught.exception.code, 'closed')
        self.assertEqual(self.reload(c).status, sm.CLOSED)
        fresh = self.new_conversation(status=sm.CLOSED, visitor='z' * 40, closed_at=conv.timezone.now())
        self.set(chat_enabled=True, chat_reopen_on_customer_message=False)
        with self.assertRaises(conv.ChatError):
            self.customer_says(fresh)

    def test_an_operator_cannot_write_into_a_closed_conversation_but_can_reopen_it(self):
        c = self.new_conversation(status=sm.CLOSED, closed_at=conv.timezone.now())
        with self.assertRaises(conv.ChatError):
            self.operator_says(c, self.op)
        with self.assertRaises(conv.ChatError):
            conv.post_message(c, sender=ChatMessage.SENDER_OPERATOR, body='یادداشت', operator=self.op, internal=True)
        c = conv.apply_rule(c, 'T18', sm.OPERATOR, operator=self.op)
        self.assertEqual((c.status, c.closed_at), (sm.WAITING_OPERATOR, None))

    def test_forbidden_manual_transitions_are_rejected_and_change_nothing(self):
        for rule, source, actor in (('T3', sm.OFFLINE, sm.OPERATOR), ('T6', sm.OFFLINE, sm.OPERATOR), ('T6', sm.WAITING_OPERATOR, sm.OPERATOR),
                                    ('T17', sm.CLOSED, sm.OPERATOR), ('T15', sm.OFFLINE, sm.OPERATOR), ('T18', sm.OFFLINE, sm.OPERATOR),
                                    ('T9', sm.ACTIVE, sm.SYSTEM), ('T3', sm.WAITING_OPERATOR, sm.CUSTOMER)):
            with self.subTest(rule=rule, source=source, actor=actor):
                c = self.new_conversation(status=source, closed_at=conv.timezone.now() if source == sm.CLOSED else None,
                                          visitor=uuid.uuid4().hex + 'q' * 8)
                with self.assertRaises(sm.InvalidTransition):
                    conv.apply_rule(c, rule, actor, operator=self.op)
                self.assertEqual(self.reload(c).status, source)

    def test_a_lost_race_raises_a_conflict_and_changes_nothing(self):
        c = self.new_conversation(status=sm.ACTIVE, assigned_operator=self.op)
        real = conv._transition_fields

        def racing(*args, **kwargs):
            fields = real(*args, **kwargs)
            Conversation.objects.filter(pk=c.pk).update(status=sm.WAITING_CUSTOMER)     # کس دیگری درست بین خواندن و نوشتن وضعیت را عوض کرد
            return fields

        with mock.patch.object(conv, '_transition_fields', side_effect=racing):
            with self.assertRaises(sm.TransitionConflict):
                conv.apply_rule(c, 'T17', sm.OPERATOR, operator=self.op)
        self.assertNotEqual(self.reload(c).status, sm.CLOSED)                             # T17 اعمال نشد (کل تراکنش برگشت)
        self.assertFalse(ChatEvent.objects.filter(conversation=c, type='T17').exists())

    # ---- یادداشت داخلی
    def test_an_internal_note_changes_neither_status_nor_unread_and_is_hidden_from_customers(self):
        c = self.new_conversation(status=sm.ACTIVE, assigned_operator=self.op)
        before = self.reload(c)
        conv.post_message(c, sender=ChatMessage.SENDER_OPERATOR, body='مشتری خوش‌حساب است', operator=self.op, internal=True)
        after = self.reload(c)
        self.assertEqual((after.status, after.unread_for_customer, after.unread_for_operator, after.last_message_preview),
                         (before.status, before.unread_for_customer, before.unread_for_operator, before.last_message_preview))
        self.assertEqual(after.last_message_seq, before.last_message_seq + 1)
        self.assertEqual([m.body for m in conv.messages_after(after, 0, include_internal=False)], ['سلام'])
        self.assertEqual(len(conv.messages_after(after, 0, include_internal=True)), 2)
        self.assertEqual(self.events(c)[-1][0], 'note')

    def test_only_operators_may_write_notes(self):
        c = self.new_conversation()
        with self.assertRaises(conv.ChatError) as caught:
            conv.post_message(c, sender=ChatMessage.SENDER_CUSTOMER, body='x', internal=True)
        self.assertEqual(caught.exception.status, 403)

    # ---- idempotency و seq
    def test_the_same_client_msg_id_never_creates_a_second_message(self):
        c = self.new_conversation()
        key = uuid.uuid4()
        first, created1 = conv.post_message(c, sender='customer', body='یک بار', client_msg_id=key)
        again, created2 = conv.post_message(c, sender='customer', body='یک بار', client_msg_id=key)
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(first.pk, again.pk)
        c = self.reload(c)
        self.assertEqual((c.last_message_seq, c.unread_for_operator), (2, 2))               # پیام اول + یک پیام، نه دو

    def test_seq_is_strictly_sequential_without_gaps_even_after_a_duplicate_attempt(self):
        c = self.new_conversation()
        for i in range(5):
            self.customer_says(c, f'پیام {i}')
        key = uuid.uuid4()
        conv.post_message(c, sender='customer', body='x', client_msg_id=key)
        conv.post_message(c, sender='customer', body='x', client_msg_id=key)
        self.assertEqual(list(ChatMessage.objects.filter(conversation=c).values_list('seq', flat=True)), list(range(1, 8)))
        self.assertEqual(self.reload(c).last_message_seq, 7)

    def test_messages_are_cleaned_and_validated(self):
        self.assertEqual(conv.clean_body('  سلام\x00\x07\r\n\r\n\r\n\r\nدنیا  ', 100), 'سلام\n\nدنیا')
        for bad, code in (('   ', 'empty'), ('', 'empty'), ('x' * 11, 'too_long')):
            with self.subTest(bad=bad), self.assertRaises(conv.ChatError) as caught:
                conv.clean_body(bad, 10)
            self.assertEqual(caught.exception.code, code)

    def test_the_preview_never_carries_links_or_newlines(self):
        c = self.new_conversation(body='ببینید https://evil.example/x?a=1\nخط دوم')
        self.assertNotIn('evil.example', c.last_message_preview)
        self.assertNotIn('\n', c.last_message_preview)
        self.assertIn('[لینک]', c.last_message_preview)

    def test_an_open_conversation_is_continued_not_duplicated(self):
        user = self.make_user()
        c1 = self.new_conversation(user=user)
        c2, _ = conv.create_offline_conversation(user=user, visitor_hash='k' * 40, name='', phone='', body='ادامه', client_msg_id=uuid.uuid4(),
                                                 source_path='/', ip='1.1.1.0')
        self.assertEqual(c1.pk, c2.pk)
        self.assertEqual(Conversation.objects.filter(user=user).count(), 1)
        self.assertEqual(self.reload(c1).last_message_seq, 2)

    def test_a_closed_conversation_beyond_the_window_starts_a_fresh_one(self):
        user = self.make_user()
        old = self.new_conversation(user=user, status=sm.CLOSED, closed_at=conv.timezone.now() - timedelta(hours=500))
        fresh, _ = conv.create_offline_conversation(user=user, visitor_hash='k' * 40, name='', phone='', body='سلام تازه', client_msg_id=uuid.uuid4(),
                                                    source_path='/', ip='1.1.1.0')
        self.assertNotEqual(old.pk, fresh.pk)
        self.assertEqual(fresh.status, sm.OFFLINE)


class ReadStateTests(ChatTestBase):
    def setUp(self):
        super().setUp()
        self.op = self.make_operator()
        self.c = self.new_conversation()
        for _ in range(3):
            self.customer_says(self.c)
        self.operator_says(self.c, self.op, 'پاسخ ۱')
        self.operator_says(self.c, self.op, 'پاسخ ۲')
        conv.post_message(self.c, sender='operator', body='یادداشت', operator=self.op, internal=True)

    def test_operator_unread_counts_only_customer_messages_after_the_watermark(self):
        c = self.reload(self.c)
        self.assertEqual(c.unread_for_operator, 4)
        c = conv.mark_read(c, 'operator', 2)
        self.assertEqual((c.last_read_seq_by_operator, c.unread_for_operator), (2, 2))
        c = conv.mark_read(c, 'operator', 99)                          # بیش از آخرین seq ← به آخرین seq برش می‌خورد
        self.assertEqual((c.last_read_seq_by_operator, c.unread_for_operator), (c.last_message_seq, 0))

    def test_customer_unread_ignores_internal_notes_and_own_messages(self):
        c = self.reload(self.c)
        self.assertEqual(c.unread_for_customer, 2)
        c = conv.mark_read(c, 'customer', 5)
        self.assertEqual((c.last_read_seq_by_customer, c.unread_for_customer), (5, 1))     # پاسخ ۲ (seq 6) هنوز نخوانده
        c = conv.mark_read(c, 'customer', 7)                                               # یادداشت داخلی (seq 7) هرگز شمرده نمی‌شود
        self.assertEqual((c.last_read_seq_by_customer, c.unread_for_customer), (7, 0))

    def test_the_watermark_never_moves_backwards_even_with_stale_tabs(self):
        c = conv.mark_read(self.reload(self.c), 'operator', 5)
        stale = conv.mark_read(c, 'operator', 2)
        self.assertEqual(stale.last_read_seq_by_operator, 5)
        again = conv.mark_read(stale, 'operator', 5)
        self.assertEqual((again.last_read_seq_by_operator, again.unread_for_operator), (5, 0))

    def test_a_corrupt_counter_heals_on_the_next_read_mark(self):
        Conversation.objects.filter(pk=self.c.pk).update(unread_for_operator=42, unread_for_customer=17)
        c = conv.mark_read(self.reload(self.c), 'operator', 0)
        self.assertEqual(c.unread_for_operator, 4)
        c = conv.mark_read(c, 'customer', 0)
        self.assertEqual(c.unread_for_customer, 2)

    def test_garbage_input_is_harmless(self):
        for value in (None, 'abc', -5, '', 3.7):
            with self.subTest(value=value):
                c = conv.mark_read(self.reload(self.c), 'customer', value)
                self.assertGreaterEqual(c.last_read_seq_by_customer, 0)
        with self.assertRaises(ValueError):
            conv.mark_read(self.c, 'nobody', 1)

    def test_a_new_message_after_a_mark_still_counts(self):
        conv.mark_read(self.reload(self.c), 'operator', 99)
        self.customer_says(self.c)
        self.assertEqual(self.reload(self.c).unread_for_operator, 1)


class PollingTests(ChatTestBase):
    def setUp(self):
        super().setUp()
        self.c = self.new_conversation()
        for i in range(60):
            self.customer_says(self.c, f'پیام {i}')
        self.c = self.reload(self.c)

    def test_messages_come_in_order_after_the_cursor_and_are_paged(self):
        first = conv.messages_after(self.c, 0, include_internal=False)
        self.assertEqual([m.seq for m in first], list(range(1, 51)))
        rest = conv.messages_after(self.c, 50, include_internal=False)
        self.assertEqual([m.seq for m in rest], list(range(51, 62)))

    def test_nothing_new_costs_no_query_at_all(self):
        with self.assertNumQueries(0):
            self.assertEqual(conv.messages_after(self.c, self.c.last_message_seq, include_internal=False), [])
            self.assertEqual(conv.messages_after(self.c, 10 ** 6, include_internal=False), [])

    def test_something_new_costs_one_indexed_query(self):
        with self.assertNumQueries(1):
            conv.messages_after(self.c, self.c.last_message_seq - 1, include_internal=False)

    def test_a_garbage_cursor_means_from_the_start(self):
        self.assertEqual(len(conv.messages_after(self.c, 'x', include_internal=False)), 50)
        self.assertEqual(len(conv.messages_after(self.c, -3, include_internal=False)), 50)
