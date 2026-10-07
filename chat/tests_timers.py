"""
فاز ۳ - جاروی تایمرها: شلیک هر تایمر با قاعده‌ی درست، پیام سیستمی/پیامک، امنیت در برابر مسابقه و شلیک دوباره،
بازگرداندن گفتگوی کارشناسِ غایب به صف، تسک Celery و زمان‌بندی Beat.
"""
from datetime import timedelta
from unittest import mock

from chat import conversations as conv
from chat import presence
from chat import statemachine as sm
from chat import timers
from chat.models import ChatEvent, ChatMessage, Conversation, OperatorPresence
from chat.testing import ChatTestBase
from django.utils import timezone


class TimerBase(ChatTestBase):
    def setUp(self):
        super().setUp()
        self.op = self.make_operator()

    def due(self, status, kind, minutes_ago=1, **extra):
        return self.new_conversation(status=status, next_timer_kind=kind,
                                     next_timer_at=timezone.now() - timedelta(minutes=minutes_ago), **extra)

    def events(self, conversation):
        return list(ChatEvent.objects.filter(conversation=conversation).order_by('id').values_list('type', 'actor_type'))


class FireTimerTests(TimerBase):
    def test_sla_expiry_in_the_queue_makes_the_conversation_offline_and_tells_the_customer(self):
        c = self.due(sm.WAITING_OPERATOR, sm.TIMER_SLA, assigned_operator=None)
        with mock.patch('chat.conversations.notify_operators_of_message') as notify, self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(timers.fire_timer(c.pk), 'T9')
        c = self.reload(c)
        self.assertEqual((c.status, c.next_timer_kind), (sm.OFFLINE, sm.TIMER_IDLE_CLOSE))
        self.assertGreater(c.next_timer_at, timezone.now() + timedelta(hours=1))
        last = ChatMessage.objects.filter(conversation=c).order_by('seq').last()
        self.assertEqual((last.sender_type, last.kind, last.seq), ('system', 'event', 2))
        self.assertIn('کارشناس آنلاینی', last.body)
        self.assertEqual((c.last_message_seq, c.unread_for_customer, c.last_message_sender), (2, 1, 'system'))
        self.assertEqual(self.events(c)[-1], ('T9', 'system'))
        self.assertEqual(notify.call_count, 1)                               # کارشناسان برای پاسخ ناهمزمان خبردار می‌شوند

    def test_sla_expiry_without_an_answer_while_active_goes_offline_too(self):
        c = self.due(sm.ACTIVE, sm.TIMER_SLA, assigned_operator=self.op)
        self.assertEqual(timers.fire_timer(c.pk), 'T10')
        self.assertEqual(self.reload(c).status, sm.OFFLINE)

    def test_no_system_message_when_the_notice_text_is_empty(self):
        self.set(chat_msg_no_operator='')
        c = self.due(sm.WAITING_OPERATOR, sm.TIMER_SLA)
        timers.fire_timer(c.pk)
        c = self.reload(c)
        self.assertEqual((c.status, c.last_message_seq, c.unread_for_customer), (sm.OFFLINE, 1, 0))

    def test_a_silent_customer_moves_active_to_waiting_customer(self):
        c = self.due(sm.ACTIVE, sm.TIMER_CUSTOMER_IDLE, assigned_operator=self.op)
        self.assertEqual(timers.fire_timer(c.pk), 'T6')
        c = self.reload(c)
        self.assertEqual((c.status, c.next_timer_kind, c.assigned_operator_id), (sm.WAITING_CUSTOMER, sm.TIMER_CUSTOMER_GONE, self.op.pk))
        self.assertEqual(c.last_message_seq, 1)                              # بدون پیام سیستمی

    def test_a_customer_who_left_makes_the_conversation_offline(self):
        c = self.due(sm.WAITING_CUSTOMER, sm.TIMER_CUSTOMER_GONE, assigned_operator=self.op)
        self.assertEqual(timers.fire_timer(c.pk), 'T8')
        c = self.reload(c)
        self.assertEqual((c.status, c.next_timer_kind), (sm.OFFLINE, sm.TIMER_IDLE_CLOSE))

    def test_an_idle_offline_conversation_is_closed_automatically(self):
        c = self.due(sm.OFFLINE, sm.TIMER_IDLE_CLOSE, unread_for_operator=1)
        self.assertEqual(timers.fire_timer(c.pk), 'T17')
        c = self.reload(c)
        self.assertEqual((c.status, c.next_timer_at, c.next_timer_kind, c.unread_for_operator), (sm.CLOSED, None, '', 0))
        self.assertIsNotNone(c.closed_at)
        self.assertEqual(self.events(c)[-1], ('T17', 'system'))

    def test_the_whole_chain_runs_with_time_travel(self):
        """ صف ← (SLA) آفلاین ← (۴۸ ساعت) بسته؛ با پاس‌دادن now به جای انتظار """
        c, _ = conv.create_live_conversation(user=None, visitor_hash='z' * 40, name='ن', phone='', body='سلام', client_msg_id=__import__('uuid').uuid4(),
                                             source_path='/', ip='1.1.1.0')
        c = self.reload(c)
        self.assertEqual((c.status, c.next_timer_kind), (sm.WAITING_OPERATOR, sm.TIMER_SLA))
        self.assertIsNone(timers.fire_timer(c.pk, now=c.next_timer_at - timedelta(seconds=1)))      # هنوز زود است
        self.assertEqual(timers.fire_timer(c.pk, now=c.next_timer_at + timedelta(seconds=1)), 'T9')
        c = self.reload(c)
        self.assertEqual(c.status, sm.OFFLINE)
        self.assertEqual(timers.fire_timer(c.pk, now=c.next_timer_at + timedelta(seconds=1)), 'T17')
        self.assertEqual(self.reload(c).status, sm.CLOSED)

    def test_not_due_closed_or_missing_do_nothing(self):
        future = self.new_conversation(status=sm.ACTIVE, next_timer_kind=sm.TIMER_SLA, next_timer_at=timezone.now() + timedelta(minutes=5))
        self.assertIsNone(timers.fire_timer(future.pk))
        closed = self.new_conversation(visitor='q' * 40, status=sm.CLOSED, next_timer_kind=sm.TIMER_SLA,
                                       next_timer_at=timezone.now() - timedelta(minutes=5))
        self.assertIsNone(timers.fire_timer(closed.pk))
        self.assertIsNone(timers.fire_timer(999999))
        none_set = self.new_conversation(visitor='r' * 40, next_timer_at=None, next_timer_kind='')
        self.assertIsNone(timers.fire_timer(none_set.pk))

    def test_a_timer_inconsistent_with_the_status_is_cleared_not_fired(self):
        c = self.due(sm.OFFLINE, sm.TIMER_SLA)                               # SLA برای وضعیت آفلاین معنا ندارد
        with self.assertLogs('chat.timers', 'WARNING'):
            self.assertIsNone(timers.fire_timer(c.pk))
        c = self.reload(c)
        self.assertEqual((c.status, c.next_timer_at, c.next_timer_kind), (sm.OFFLINE, None, ''))

    def test_firing_twice_changes_things_once(self):
        c = self.due(sm.WAITING_OPERATOR, sm.TIMER_SLA)
        self.assertEqual(timers.fire_timer(c.pk), 'T9')
        self.assertIsNone(timers.fire_timer(c.pk))
        self.assertEqual([e for e in self.events(c) if e[0] == 'T9'], [('T9', 'system')])
        self.assertEqual(ChatMessage.objects.filter(conversation=c, sender_type='system').count(), 1)

    def test_a_customer_message_racing_the_timer_wins(self):
        """ بین خواندن و نوشتن، مشتری پیام می‌دهد (تایمر عوض می‌شود) ← شلیک بی‌اثر """
        c = self.due(sm.ACTIVE, sm.TIMER_CUSTOMER_IDLE, assigned_operator=self.op)
        real_cas = timers._cas

        def racing(conversation, fields, extra_filter=None):
            Conversation.objects.filter(pk=c.pk).update(next_timer_kind=sm.TIMER_SLA, next_timer_at=timezone.now() + timedelta(minutes=10))
            return real_cas(conversation, fields, extra_filter)

        with mock.patch('chat.timers._cas', racing):
            self.assertIsNone(timers.fire_timer(c.pk))
        c = self.reload(c)
        self.assertEqual((c.status, c.next_timer_kind), (sm.ACTIVE, sm.TIMER_SLA))
        self.assertNotIn('T6', [e[0] for e in self.events(c)])

    def test_an_operator_action_racing_the_timer_wins(self):
        c = self.due(sm.WAITING_OPERATOR, sm.TIMER_SLA)
        real_cas = timers._cas

        def racing(conversation, fields, extra_filter=None):
            Conversation.objects.filter(pk=c.pk).update(status=sm.ACTIVE, assigned_operator=self.op)
            return real_cas(conversation, fields, extra_filter)

        with mock.patch('chat.timers._cas', racing):
            self.assertIsNone(timers.fire_timer(c.pk))
        self.assertEqual(self.reload(c).status, sm.ACTIVE)
        self.assertEqual(ChatMessage.objects.filter(conversation=c, sender_type='system').count(), 0)

    def test_a_customer_reply_after_the_sla_notice_continues_the_conversation(self):
        c = self.due(sm.WAITING_OPERATOR, sm.TIMER_SLA)
        timers.fire_timer(c.pk)
        message = self.customer_says(self.reload(c), 'دوباره سلام')
        self.assertEqual(message.seq, 3)                                     # پیام سیستمی seq=2 را گرفته؛ ترتیب سالم
        self.assertEqual(self.reload(c).status, sm.OFFLINE)                  # کارشناس آنلاین نیست ← T12


class SweepTests(TimerBase):
    def test_only_due_conversations_fire_and_counts_are_reported(self):
        self.due(sm.WAITING_OPERATOR, sm.TIMER_SLA)
        self.due(sm.ACTIVE, sm.TIMER_CUSTOMER_IDLE, visitor='b' * 40)
        self.due(sm.OFFLINE, sm.TIMER_IDLE_CLOSE, visitor='c' * 40)
        not_due = self.new_conversation(visitor='d' * 40, status=sm.ACTIVE, next_timer_kind=sm.TIMER_SLA,
                                        next_timer_at=timezone.now() + timedelta(hours=1))
        self.assertEqual(timers.run_due_timers(), {'T9': 1, 'T6': 1, 'T17': 1})
        self.assertEqual(self.reload(not_due).status, sm.ACTIVE)
        self.assertEqual(timers.run_due_timers(), {})                        # دومین اجرا بی‌اثر

    def test_one_broken_conversation_does_not_stop_the_rest(self):
        a = self.due(sm.WAITING_OPERATOR, sm.TIMER_SLA, minutes_ago=5)
        b = self.due(sm.WAITING_OPERATOR, sm.TIMER_SLA, minutes_ago=1, visitor='b' * 40)
        real = timers.fire_timer

        def flaky(pk, now=None):
            if pk == a.pk:
                raise RuntimeError('boom')
            return real(pk, now)

        with mock.patch('chat.timers.fire_timer', flaky), self.assertLogs('chat.timers', 'ERROR'):
            self.assertEqual(timers.run_due_timers(), {'T9': 1})
        self.assertEqual(self.reload(a).status, sm.WAITING_OPERATOR)
        self.assertEqual(self.reload(b).status, sm.OFFLINE)

    def test_the_sweeper_is_indexed_and_cheap_when_nothing_is_due(self):
        self.new_conversation(status=sm.ACTIVE, next_timer_kind=sm.TIMER_SLA, next_timer_at=timezone.now() + timedelta(hours=1))
        with self.assertNumQueries(1):
            self.assertEqual(timers.run_due_timers(), {})


class AssigneeAbsenceTests(TimerBase):
    def active(self, operator, minutes_idle=30, **extra):
        return self.new_conversation(status=sm.ACTIVE, assigned_operator=operator,
                                     last_activity_at=timezone.now() - timedelta(minutes=minutes_idle), **extra)

    def test_an_absent_assignee_with_no_recent_activity_releases_the_conversation(self):
        c = self.active(self.op)
        self.assertEqual(timers.release_absent_assignees(), 1)
        c = self.reload(c)
        self.assertEqual((c.status, c.assigned_operator_id, c.next_timer_kind), (sm.WAITING_OPERATOR, None, sm.TIMER_SLA))
        event = ChatEvent.objects.filter(conversation=c, type='T15').get()
        self.assertEqual((event.actor_type, event.meta), ('system', {'reason': 'assignee_absent'}))

    def test_an_online_assignee_keeps_the_conversation(self):
        c = self.active(self.op)
        presence.heartbeat(self.op, online=True)
        self.assertEqual(timers.release_absent_assignees(), 0)
        self.assertEqual(self.reload(c).status, sm.ACTIVE)

    def test_recent_activity_protects_the_conversation(self):
        c = self.active(self.op, minutes_idle=2)
        self.assertEqual(timers.release_absent_assignees(), 0)
        self.assertEqual(self.reload(c).status, sm.ACTIVE)

    def test_an_assignee_who_only_just_went_offline_is_not_released_yet(self):
        c = self.active(self.op)
        presence.heartbeat(self.op, online=True)
        presence.heartbeat(self.op, online=False)                            # آخرین نبض همین الان
        self.assertEqual(timers.release_absent_assignees(), 0)
        OperatorPresence.objects.update(last_seen=timezone.now() - timedelta(minutes=20))
        self.assertEqual(timers.release_absent_assignees(), 1)
        self.assertEqual(self.reload(c).status, sm.WAITING_OPERATOR)

    def test_only_active_conversations_are_released(self):
        waiting = self.new_conversation(status=sm.WAITING_CUSTOMER, assigned_operator=self.op,
                                        last_activity_at=timezone.now() - timedelta(hours=2))
        self.assertEqual(timers.release_absent_assignees(), 0)
        self.assertEqual(self.reload(waiting).assigned_operator_id, self.op.pk)

    def test_another_present_operator_does_not_save_an_absent_ones_conversation(self):
        other = self.make_operator()
        presence.heartbeat(other, online=True)
        c = self.active(self.op)
        self.assertEqual(timers.release_absent_assignees(), 1)
        self.assertEqual(self.reload(c).status, sm.WAITING_OPERATOR)


class TaskAndScheduleTests(TimerBase):
    def test_the_celery_task_sweeps_and_returns_a_summary(self):
        from chat.tasks import sweep_chat_timers
        self.due(sm.WAITING_OPERATOR, sm.TIMER_SLA)
        self.assertEqual(sweep_chat_timers()['fired'], {'T9': 1})

    def test_the_task_sweeps_even_when_the_widget_is_switched_off(self):
        from chat.tasks import sweep_chat_timers
        c = self.due(sm.OFFLINE, sm.TIMER_IDLE_CLOSE)
        self.set(chat_enabled=False)
        sweep_chat_timers()
        self.assertEqual(self.reload(c).status, sm.CLOSED)

    def test_overlapping_sweeps_are_skipped(self):
        from chat import cache as chatcache
        from chat.tasks import sweep_chat_timers
        self.due(sm.WAITING_OPERATOR, sm.TIMER_SLA)
        self.assertTrue(chatcache.acquire('sweep:lock', 25))
        self.assertEqual(sweep_chat_timers(), {'skipped': 'locked'})
        chatcache.forget('sweep:lock')
        self.assertEqual(sweep_chat_timers()['fired'], {'T9': 1})

    def test_the_task_is_registered_and_scheduled_every_thirty_seconds(self):
        from config.celery import app, setup_chat_timer_schedule
        app.loader.import_default_modules()
        self.assertIn('chat.tasks.sweep_chat_timers', app.tasks)
        sender = mock.Mock()
        sender.signature.side_effect = lambda name: name
        setup_chat_timer_schedule(sender)
        args, kwargs = sender.add_periodic_task.call_args
        self.assertEqual((args[0], args[1], kwargs['name']), (30.0, 'chat.tasks.sweep_chat_timers', 'sweep-chat-timers'))
