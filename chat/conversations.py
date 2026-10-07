"""
عملیات دامنه‌ی گفتگو: ساخت، ارسال پیام، انتقال وضعیت، خوانده‌شدن، خواندن پیام‌ها. تنها مسیر تغییر وضعیت و نوشتن پیام همین‌جاست
(ماتریس: chat/statemachine.py).

اصول:
  - ارسال پیام: داخل یک تراکنش، اول ردیف گفتگو با UPDATE (last_message_seq += 1) قفل می‌شود، بعد پیام با همان seq درج می‌شود.
    قفل تا commit می‌ماند؛ پس seq دقیقاً به ترتیب commit تخصیص می‌یابد و پولینگِ after=<seq> هیچ پیامی را از دست نمی‌دهد.
    (اول والد، بعد فرزند: همان الگوی ضد-deadlock رزرو موجودی.)
  - ارسال idempotent است (client_msg_id): تکرار همان درخواست پیام دوم نمی‌سازد.
  - انتقال وضعیت: مقایسه-و-تعویض اتمیک (UPDATE ... WHERE status=مبدأ)؛ مسابقه ← TransitionConflict.
  - دیتابیس مرجع نهایی است؛ Redis فقط برای محدودیت نرخ، cooldown پیامک و نشانگر «مشتری آنلاین است».
"""
import re
import uuid
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from . import statemachine as sm
from .availability import live_available
from .models import ChatEvent, ChatMessage, Conversation
from .notifier import notify_customer_of_reply, notify_operators_of_message
from .settingsio import cfg as load_cfg
from .text import safe_inline

_CONTROL_CHARS = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
PAGE_SIZE = 50
GUEST_ATTACH_DAYS = 30


class ChatError(Exception):
    """ خطای قابل‌نمایش به کلاینت: code ماشینی، message فارسی، status HTTP """

    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def clean_body(text, max_length):
    value = _CONTROL_CHARS.sub('', str(text or '')).replace('\r\n', '\n').replace('\r', '\n').strip()
    value = re.sub(r'\n{3,}', '\n\n', value)
    if not value:
        raise ChatError('empty', 'متن پیام را بنویسید.')
    if len(value) > max_length:
        raise ChatError('too_long', f'پیام حداکثر {max_length} نویسه است.')
    return value


def _cfg():
    return load_cfg()


# ------------------------------------------------------------------ رویداد و اثر انتقال

def _log(conversation_id, rule_id, actor, actor_id, from_status, to_status, meta=None):
    ChatEvent.objects.create(
        conversation_id=conversation_id, type=rule_id, actor_type=actor, actor_id=actor_id,
        from_status=from_status or '', to_status=to_status or '', meta=meta or {})


def _transition_fields(rule_id, conversation, now, cfg, operator=None, new_operator=None):
    """ فیلدهای وضعیت/مسئول/تایمر/زمان‌های بسته‌شدن بعد از قاعده (فقط آنچه قاعده تعیین می‌کند) """
    rule = sm.RULES[rule_id]
    fields = {'status': rule.target}
    timer_at, timer_kind = sm.timer_for(rule_id, now, cfg, conversation.next_timer_kind, conversation.next_timer_at)
    fields['next_timer_at'], fields['next_timer_kind'] = timer_at, timer_kind

    if rule_id in ('T3', 'T13'):
        fields['assigned_operator'] = operator
    elif rule_id in ('T4', 'T14'):
        if conversation.assigned_operator_id is None:
            fields['assigned_operator'] = operator
    elif rule_id == 'T16':
        fields['assigned_operator'] = new_operator
    elif rule_id in ('T15', 'T11', 'T18'):
        fields['assigned_operator'] = None
        if rule_id == 'T18':
            fields['closed_at'] = None
    elif rule_id == 'T17':
        fields['closed_at'] = now
        fields['unread_for_operator'] = 0
    return fields


def _cas(conversation, fields, extra_filter=None):
    """ UPDATE ... WHERE id AND status=مبدأ؛ اگر وضعیت عوض شده بود TransitionConflict """
    query = Conversation.objects.filter(pk=conversation.pk, status=conversation.status)
    if extra_filter:
        query = query.filter(**extra_filter)
    if query.update(**fields) != 1:
        raise sm.TransitionConflict('وضعیت گفتگو همین الان عوض شد؛ دوباره تلاش کنید.')


def apply_rule(conversation, rule_id, actor, *, operator=None, new_operator=None, now=None):
    """
    انتقال بدون پیام (برداشتن، منتظر مشتری، بازگرداندن به صف، ارجاع، بستن، بازگشایی). ماتریس را اعمال می‌کند، CAS می‌زند و رخداد
    ثبت می‌کند. ← گفتگوی به‌روز. InvalidTransition / TransitionConflict.
    """
    now = now or timezone.now()
    cfg = _cfg()
    with transaction.atomic():
        fresh = Conversation.objects.select_related('user').get(pk=conversation.pk)
        sm.check(rule_id, fresh.status, actor)
        fields = _transition_fields(rule_id, fresh, now, cfg, operator=operator, new_operator=new_operator)
        fields['last_activity_at'] = now
        _cas(fresh, fields)
        _log(fresh.pk, rule_id, actor, getattr(operator, 'pk', None), fresh.status, fields['status'],
             {'new_operator': getattr(new_operator, 'pk', None)} if new_operator else None)
    return Conversation.objects.select_related('user', 'assigned_operator').get(pk=conversation.pk)


# ------------------------------------------------------------------ ارسال پیام

def _rule_for_message(conversation, sender, cfg, now):
    status = conversation.status
    if sender == ChatMessage.SENDER_CUSTOMER:
        if status == sm.WAITING_OPERATOR:
            return 'T20'
        if status == sm.ACTIVE:
            return 'T5'
        if status == sm.WAITING_CUSTOMER:
            return 'T7' if conversation.assigned_operator_id else 'T7b'
        if status == sm.OFFLINE:
            return 'T11' if live_available(cfg, now) else 'T12'
        return 'T18'                                           # بسته: بازگشایی (پنجره/تنظیم جداگانه بررسی می‌شود)
    if sender == ChatMessage.SENDER_OPERATOR:
        return {sm.WAITING_OPERATOR: 'T3', sm.ACTIVE: 'T4', sm.WAITING_CUSTOMER: 'T14', sm.OFFLINE: 'T13'}.get(status)
    return None


def post_message(conversation, *, sender, body, client_msg_id=None, operator=None, internal=False, now=None):
    """
    پیام تازه را در گفتگو ثبت می‌کند و انتقال وضعیت مربوطه را اعمال می‌کند. ← (پیام، ساخته‌شد؟)
    ساخته‌شد=False یعنی همین client_msg_id قبلاً ثبت شده بود (idempotent).
    یادداشت داخلی (internal) فقط کارشناس؛ وضعیت و unread را عوض نمی‌کند.
    """
    now = now or timezone.now()
    client_msg_id = client_msg_id or uuid.uuid4()
    existing = ChatMessage.objects.filter(conversation_id=conversation.pk, client_msg_id=client_msg_id).first()
    if existing:
        return existing, False
    if internal and sender != ChatMessage.SENDER_OPERATOR:
        raise ChatError('forbidden', 'یادداشت داخلی فقط برای کارشناس است.', 403)
    cfg = _cfg()

    try:
        with transaction.atomic():
            # ۱) قفل والد با UPDATE (seq به ترتیب commit تخصیص می‌یابد)
            Conversation.objects.filter(pk=conversation.pk).update(last_message_seq=F('last_message_seq') + 1)
            locked = Conversation.objects.select_related('user').get(pk=conversation.pk)
            seq = locked.last_message_seq

            rule_id = None
            if not internal:
                rule_id = _rule_for_message(locked, sender, cfg, now)
                if rule_id == 'T18':
                    _check_reopen(locked, sender, cfg, now)
                if rule_id is None:
                    raise ChatError('closed', 'این گفتگو بسته است.', 409)
                sm.check(rule_id, locked.status, sender)
            elif locked.status == sm.CLOSED:
                raise ChatError('closed', 'این گفتگو بسته است.', 409)

            # ۲) فرزند
            message = ChatMessage.objects.create(
                conversation_id=locked.pk, seq=seq, sender_type=sender, operator=operator, body=body,
                client_msg_id=client_msg_id, is_internal_note=internal)

            fields = {'last_activity_at': now}
            if not internal:
                fields.update({
                    'last_message_at': now, 'last_message_sender': sender,
                    'last_message_preview': safe_inline(body, 140),
                })
                if sender == ChatMessage.SENDER_CUSTOMER:
                    fields['unread_for_operator'] = F('unread_for_operator') + 1
                else:
                    fields['unread_for_customer'] = F('unread_for_customer') + 1
                fields.update(_transition_fields(rule_id, locked, now, cfg, operator=operator))
                _cas(locked, fields)
                _log(locked.pk, rule_id, sender, getattr(operator, 'pk', None), locked.status, fields['status'], {'seq': seq})
            else:
                Conversation.objects.filter(pk=locked.pk).update(**fields)
                _log(locked.pk, 'note', sender, getattr(operator, 'pk', None), locked.status, locked.status, {'seq': seq})

            if not internal:
                transaction.on_commit(lambda: _after_commit(locked.pk, rule_id, sender))
    except IntegrityError:
        existing = ChatMessage.objects.filter(conversation_id=conversation.pk, client_msg_id=client_msg_id).first()
        if existing:
            return existing, False
        raise
    return message, True


def _check_reopen(conversation, sender, cfg, now):
    """ T18 با پیام مشتری: فقط داخل مهلت بازگشایی و اگر تنظیم اجازه می‌دهد """
    if not cfg.chat_reopen_on_customer_message:
        raise ChatError('closed', 'این گفتگو بسته شده است؛ گفتگوی تازه‌ای شروع کنید.', 409)
    closed_at = conversation.closed_at or conversation.last_activity_at or now
    if now - closed_at > timedelta(hours=int(cfg.chat_reopen_window_hours)):
        raise ChatError('closed', 'این گفتگو بسته شده است؛ گفتگوی تازه‌ای شروع کنید.', 409)


def _after_commit(conversation_id, rule_id, sender):
    """ بعد از commit: پیامک‌ها (خطا نمی‌اندازد) """
    conversation = Conversation.objects.select_related('user').filter(pk=conversation_id).first()
    if conversation is None:
        return
    cfg = _cfg()
    if sender == ChatMessage.SENDER_CUSTOMER:
        if conversation.status == sm.OFFLINE:            # پیام ناهمزمان تازه؛ گفتگوی زنده در فاز ۳ (chat_new_conversation_admin)
            notify_operators_of_message(conversation, cfg)
    elif sender == ChatMessage.SENDER_OPERATOR and rule_id in ('T13', 'T14'):
        notify_customer_of_reply(conversation, cfg)


# ------------------------------------------------------------------ ساخت گفتگو (پیام آفلاین، T2)

def create_offline_conversation(*, user, visitor_hash, name, phone, body, client_msg_id, source_path, ip, now=None):
    """
    اولین پیام یک بازدیدکننده از فرم آفلاین (T2). ← (گفتگو، پیام). گفتگوی باز موجود همان بازدیدکننده/کاربر ادامه می‌یابد (یک
    گفتگوی باز برای هر بازدیدکننده)؛ گفتگوی بسته‌ی داخل مهلت بازگشایی با T18 باز می‌شود.
    """
    now = now or timezone.now()
    cfg = _cfg()
    existing = _latest_for(user, visitor_hash)
    if existing is not None and existing.status != sm.CLOSED:
        message, _ = post_message(existing, sender=ChatMessage.SENDER_CUSTOMER, body=body, client_msg_id=client_msg_id, now=now)
        return Conversation.objects.select_related('user').get(pk=existing.pk), message
    if existing is not None:
        try:
            _check_reopen(existing, ChatMessage.SENDER_CUSTOMER, cfg, now)
        except ChatError:
            pass                                                # مهلت گذشته یا بازگشایی خاموش: گفتگوی تازه
        else:
            message, _ = post_message(existing, sender=ChatMessage.SENDER_CUSTOMER, body=body, client_msg_id=client_msg_id, now=now)
            return Conversation.objects.select_related('user').get(pk=existing.pk), message

    with transaction.atomic():
        conversation = Conversation.objects.create(
            visitor_hash=visitor_hash, user=user, guest_name=name if not user else '', guest_phone=phone if not user else '',
            channel_origin=Conversation.CHANNEL_OFFLINE, status=sm.OFFLINE, source_path=source_path, client_ip_trunc=ip,
            last_activity_at=now)
        # T2 ثبت رخداد و تایمر ساخت؛ پیام اول با post_message (T12 معادل نیست) ← مستقیم همین‌جا
        Conversation.objects.filter(pk=conversation.pk).update(last_message_seq=F('last_message_seq') + 1)
        locked = Conversation.objects.select_related('user').get(pk=conversation.pk)
        message = ChatMessage.objects.create(
            conversation_id=locked.pk, seq=locked.last_message_seq, sender_type=ChatMessage.SENDER_CUSTOMER, body=body,
            client_msg_id=client_msg_id)
        timer_at, timer_kind = sm.timer_for('T2', now, cfg)
        Conversation.objects.filter(pk=locked.pk).update(
            last_message_at=now, last_message_sender=ChatMessage.SENDER_CUSTOMER, last_message_preview=safe_inline(body, 140),
            unread_for_operator=1, next_timer_at=timer_at, next_timer_kind=timer_kind)
        _log(locked.pk, 'T2', sm.CUSTOMER, getattr(user, 'pk', None), '', sm.OFFLINE, {'seq': 1})
        transaction.on_commit(lambda: _after_commit(locked.pk, 'T12', ChatMessage.SENDER_CUSTOMER))
    return Conversation.objects.select_related('user').get(pk=conversation.pk), message


def _latest_for(user, visitor_hash):
    from django.db.models import Q

    query = Q()
    if user is not None and getattr(user, 'is_authenticated', False):
        query |= Q(user=user)
    if visitor_hash:
        query |= Q(visitor_hash=visitor_hash, user__isnull=True)
    if not query:
        return None
    return Conversation.objects.filter(query).select_related('user').order_by('-id').first()


# ------------------------------------------------------------------ خوانده‌شدن و خواندن پیام‌ها

def mark_read(conversation, side, upto_seq):
    """
    واترمارک خوانده‌شدن (یکنواخت: فقط بالا می‌رود) و بازمحاسبه‌ی unread از روی حقیقت. داخل تراکنش: UPDATE واترمارک ردیف را قفل می‌کند،
    پس پیام تازه‌ای که هم‌زمان می‌رسد تا commit منتظر می‌ماند و شمارنده کم‌شمار نمی‌شود. چند تب هم‌زمان اختلالی ایجاد نمی‌کنند.
    """
    if side not in ('customer', 'operator'):
        raise ValueError(side)
    watermark = 'last_read_seq_by_customer' if side == 'customer' else 'last_read_seq_by_operator'
    unread = 'unread_for_customer' if side == 'customer' else 'unread_for_operator'
    other = (ChatMessage.SENDER_OPERATOR, ChatMessage.SENDER_SYSTEM) if side == 'customer' else (ChatMessage.SENDER_CUSTOMER,)
    try:
        upto = max(0, int(upto_seq))
    except (TypeError, ValueError):
        upto = 0
    with transaction.atomic():
        current = Conversation.objects.filter(pk=conversation.pk).values_list('last_message_seq', watermark).first()
        if current is None:
            return conversation
        upto = min(upto, current[0])
        Conversation.objects.filter(pk=conversation.pk, **{f'{watermark}__lt': upto}).update(**{watermark: upto})
        # قفل ردیف حتی وقتی واترمارک عوض نشد (تا شمارش و نوشتن اتمیک باشد)
        Conversation.objects.filter(pk=conversation.pk).update(last_activity_at=F('last_activity_at'))
        mark = Conversation.objects.filter(pk=conversation.pk).values_list(watermark, flat=True).first()
        count = ChatMessage.objects.filter(conversation_id=conversation.pk, seq__gt=mark, sender_type__in=other,
                                           is_internal_note=False).count()
        Conversation.objects.filter(pk=conversation.pk).update(**{unread: count})
    return Conversation.objects.get(pk=conversation.pk)


def messages_after(conversation, after, *, include_internal):
    """ پیام‌های با seq > after به ترتیب؛ حداکثر PAGE_SIZE. کارشناس یادداشت داخلی هم می‌بیند، مشتری هرگز. """
    try:
        after = max(0, int(after))
    except (TypeError, ValueError):
        after = 0
    if after >= conversation.last_message_seq:
        return []                                     # مرجع: ستون خودِ DB که همراه خواندن گفتگو آمده؛ بدون کوئری اضافه
    query = ChatMessage.objects.filter(conversation_id=conversation.pk, seq__gt=after).select_related('operator')
    if not include_internal:
        query = query.filter(is_internal_note=False)
    return list(query.order_by('seq')[:PAGE_SIZE])
