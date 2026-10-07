"""
اجرای تایمرهای ماشین وضعیت (جاروی Celery Beat، chat/tasks.py). هر گفتگو حداکثر یک تایمر دارد (next_timer_at/next_timer_kind) که
chat/statemachine.timer_for آن را بعد از هر قاعده تنظیم کرده است؛ این‌جا فقط تایمرهای سررسیده «شلیک» می‌شوند:

  وضعیت             نوع تایمر        قاعده
  waiting_operator  sla              T9  (SLA در صف منقضی ← آفلاین، پیام سیستمی به مشتری و پیامک به کارشناسان)
  active            sla              T10 (مشتری پیام داده و کارشناس در مهلت پاسخ نداد ← آفلاین، مثل بالا)
  active            customer_idle    T6  (کارشناس پاسخ داده و مشتری ساکت است ← منتظر مشتری)
  waiting_customer  customer_gone    T8  (مشتری رفته ← آفلاین)
  offline           idle_close       T17 (بستن خودکار گفتگوی بی‌فعالیت)

شلیک امن در برابر مسابقه است: داخل تراکنش دوباره می‌خوانیم، سررسید و نوع تایمر را بررسی می‌کنیم و انتقال با مقایسه-و-تعویضِ
(وضعیت + موعد تایمر) انجام می‌شود؛ اگر هم‌زمان مشتری پیام داده یا کارشناس اقدام کرده، هیچ اتفاقی نمی‌افتد. دو جاروی هم‌زمان هم
یک گفتگو را دوبار جابه‌جا نمی‌کنند. تایمرِ ناسازگار با وضعیت (نباید پیش بیاید) پاک می‌شود تا جارو گیر نکند.

علاوه بر تایمرها: گفتگوی ACTIVE که کارشناس مسئولش بیش از chat_assignee_timeout_minutes آفلاین/بی‌نبض بوده و در این مدت هیچ فعالیتی
در گفتگو نبوده، با T15 (عامل سیستم) به صف برمی‌گردد.
"""
import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from . import presence
from . import statemachine as sm
from .conversations import _after_commit, _cas, _log, _transition_fields, apply_rule
from .models import ChatMessage, Conversation
from .settingsio import cfg as load_cfg
from .text import safe_inline

logger = logging.getLogger(__name__)

BATCH = 200
MAX_BATCHES = 5

# (وضعیت، نوع تایمر) ← قاعده
TIMER_RULES = {
    (sm.WAITING_OPERATOR, sm.TIMER_SLA): 'T9',
    (sm.ACTIVE, sm.TIMER_SLA): 'T10',
    (sm.ACTIVE, sm.TIMER_CUSTOMER_IDLE): 'T6',
    (sm.WAITING_CUSTOMER, sm.TIMER_CUSTOMER_GONE): 'T8',
    (sm.OFFLINE, sm.TIMER_IDLE_CLOSE): 'T17',
}
NOTIFY_RULES = ('T9', 'T10')


def fire_timer(conversation_id, now=None):
    """ تایمر سررسیده‌ی یک گفتگو را شلیک می‌کند. ← شناسه‌ی قاعده‌ی اجراشده، یا None (چیزی برای انجام نبود/مسابقه‌ی بازنده) """
    now = now or timezone.now()
    cfg = load_cfg()
    rule_id = None
    with transaction.atomic():
        fresh = Conversation.objects.select_related('user').filter(pk=conversation_id).first()
        if fresh is None or fresh.status == sm.CLOSED or fresh.next_timer_at is None or fresh.next_timer_at > now:
            return None
        rule_id = TIMER_RULES.get((fresh.status, fresh.next_timer_kind))
        if rule_id is None:
            logger.warning('تایمر ناسازگار با وضعیت گفتگو %s (%s/%s)؛ پاک شد.', fresh.pk, fresh.status, fresh.next_timer_kind)
            Conversation.objects.filter(pk=fresh.pk, next_timer_at=fresh.next_timer_at).update(next_timer_at=None, next_timer_kind='')
            return None

        fields = _transition_fields(rule_id, fresh, now, cfg)
        fields['last_activity_at'] = now
        notice = ''
        if rule_id in NOTIFY_RULES:
            notice = safe_inline(cfg.chat_msg_no_operator, 500) if cfg.chat_msg_no_operator else ''
            if notice:
                fields.update({'last_message_seq': F('last_message_seq') + 1, 'unread_for_customer': F('unread_for_customer') + 1,
                               'last_message_at': now, 'last_message_sender': ChatMessage.SENDER_SYSTEM,
                               'last_message_preview': safe_inline(notice, 140)})
        try:
            _cas(fresh, fields, extra_filter={'next_timer_at': fresh.next_timer_at, 'next_timer_kind': fresh.next_timer_kind})
        except sm.TransitionConflict:
            return None                                          # هم‌زمان چیزی عوض شد؛ تایمر دیگر مال این وضعیت نیست
        seq = None
        if notice:
            seq = Conversation.objects.filter(pk=fresh.pk).values_list('last_message_seq', flat=True).get()
            ChatMessage.objects.create(conversation_id=fresh.pk, seq=seq, sender_type=ChatMessage.SENDER_SYSTEM, body=notice,
                                       kind=ChatMessage.KIND_EVENT)
        meta = {'timer': fresh.next_timer_kind}
        if seq:
            meta['seq'] = seq
        _log(fresh.pk, rule_id, sm.SYSTEM, None, fresh.status, fields['status'], meta)
        transaction.on_commit(lambda: _after_commit(fresh.pk, rule_id, sm.SYSTEM))
    return rule_id


def run_due_timers(now=None):
    """ همه‌ی تایمرهای سررسیده (دسته‌ای) ← {قاعده: تعداد} """
    now = now or timezone.now()
    fired = {}
    for _ in range(MAX_BATCHES):
        ids = list(Conversation.objects.filter(next_timer_at__lte=now).exclude(status=sm.CLOSED)
                   .order_by('next_timer_at').values_list('pk', flat=True)[:BATCH])
        if not ids:
            break
        progressed = 0
        for pk in ids:
            try:
                rule_id = fire_timer(pk, now)
            except Exception:  # noqa: BLE001 - یک گفتگوی خراب جاروی بقیه را نمی‌شکند
                logger.exception('شلیک تایمر گفتگو %s ناموفق بود.', pk)
                continue
            if rule_id:
                fired[rule_id] = fired.get(rule_id, 0) + 1
                progressed += 1
        if len(ids) < BATCH or not progressed:
            break                                               # دسته‌ی ناقص یا بدون پیشرفت ← بار بعد (جلوگیری از چرخه‌ی بی‌پایان)
    return fired


def release_absent_assignees(now=None):
    """
    گفتگوی ACTIVE که کارشناس مسئولش غایب است (آفلاین یا بی‌نبض) و مدتِ chat_assignee_timeout_minutes هیچ فعالیتی نداشته، با T15
    (عامل سیستم) به صف برمی‌گردد. فقط ACTIVE: گفتگوی «منتظر مشتری» توپ در زمین مشتری است و غیبت کارشناس مشکلی نمی‌سازد.
    """
    now = now or timezone.now()
    cfg = load_cfg()
    minutes = int(cfg.chat_assignee_timeout_minutes or 0)
    if minutes <= 0:
        return 0
    cutoff = now - timedelta(minutes=minutes)
    candidates = list(Conversation.objects.filter(status=sm.ACTIVE, assigned_operator__isnull=False, last_activity_at__lte=cutoff)[:BATCH])
    if not candidates:
        return 0
    present = presence.present_operator_ids(cfg, now, {c.assigned_operator_id for c in candidates})
    released = 0
    for conversation in candidates:
        if conversation.assigned_operator_id in present:
            continue
        # نبضِ تازه‌تر از cutoff یعنی غیبت هنوز به مهلت نرسیده (کارشناس شاید همین الان آفلاین شده)
        row = presence.OperatorPresence.objects.filter(operator_id=conversation.assigned_operator_id).first()
        if row is not None and row.last_seen and row.last_seen > cutoff:
            continue
        try:
            apply_rule(conversation, 'T15', sm.SYSTEM, now=now, meta={'reason': 'assignee_absent'})
            released += 1
        except (sm.InvalidTransition, sm.TransitionConflict):
            continue                                            # هم‌زمان عوض شد؛ مشکلی نیست
        except Exception:  # noqa: BLE001
            logger.exception('بازگرداندن گفتگوی %s به صف ناموفق بود.', conversation.pk)
    return released
