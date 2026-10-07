"""
پیشخوان کارشناس (API JSON زیر /admin/chat/conversation/console/api/…). دسترسی: کاربر staff که مجوز chat.operate_chat دارد
(گروه «کارشناس پشتیبانی») یا superuser. هر تغییر وضعیت از chat/statemachine.py و chat/conversations.py عبور می‌کند.
"""
import logging

from django.contrib.auth import get_user_model
from django.db.models import F, Q
from django.http import Http404, JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import attachments, blocks
from . import cache as chatcache
from . import conversations as conv
from . import presence
from . import statemachine as sm
from .api import fail, ok, parse_uuid, read_payload
from .models import ChatAttachment, ChatMessage, Conversation, QuickReply
from .serializers import iso, message_for_operator
from .settingsio import cfg as load_cfg

logger = logging.getLogger(__name__)

INBOX_LIMIT = 100
FILTERS = ('all', 'waiting', 'mine', 'offline', 'unread', 'closed')


def is_operator(user):
    return bool(user.is_active and user.is_staff and (user.is_superuser or user.has_perm('chat.operate_chat')))


def operator_required(view):
    def wrapped(request, *args, **kwargs):
        if not is_operator(request.user):
            return fail('forbidden', 'دسترسی ندارید.', 403)
        return view(request, *args, **kwargs)
    wrapped.__name__ = view.__name__
    return wrapped


def operator_name(user):
    if user is None:
        return ''
    return (f'{user.first_name or ""} {user.last_name or ""}'.strip()) or user.phone_number


def conversation_row(c):
    return {
        'id': c.pk, 'name': c.display_name, 'phone': c.display_phone, 'status': c.status, 'channel': c.channel_origin,
        'unread': c.unread_for_operator, 'preview': c.last_message_preview, 'last_sender': c.last_message_sender,
        'last_message_at': iso(c.last_message_at or c.created_at), 'assigned': operator_name(c.assigned_operator) if c.assigned_operator_id else '',
        'assigned_id': c.assigned_operator_id, 'timer_at': iso(c.next_timer_at), 'timer_kind': c.next_timer_kind,
        'registered': bool(c.user_id),
    }


def _filtered(request, name):
    qs = Conversation.objects.select_related('user', 'assigned_operator')
    if name == 'closed':
        return qs.filter(status=sm.CLOSED)
    qs = qs.exclude(status=sm.CLOSED)
    if name == 'waiting':
        qs = qs.filter(status__in=(sm.WAITING_OPERATOR, sm.ACTIVE), unread_for_operator__gt=0)
    elif name == 'mine':
        qs = qs.filter(assigned_operator=request.user)
    elif name == 'offline':
        qs = qs.filter(status=sm.OFFLINE)
    elif name == 'unread':
        qs = qs.filter(unread_for_operator__gt=0)
    return qs


@require_GET
@never_cache
@operator_required
def inbox_api(request):
    name = request.GET.get('filter', 'all')
    if name not in FILTERS:
        name = 'all'
    qs = _filtered(request, name)
    q = (request.GET.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(guest_name__icontains=q) | Q(guest_phone__icontains=q) | Q(user__phone_number__icontains=q) |
                       Q(user__first_name__icontains=q) | Q(user__last_name__icontains=q) | Q(last_message_preview__icontains=q))
    rows = [conversation_row(c) for c in qs.order_by(F('last_message_at').desc(nulls_last=True), '-id')[:INBOX_LIMIT]]
    open_qs = Conversation.objects.exclude(status=sm.CLOSED)
    counts = {
        'unread': open_qs.filter(unread_for_operator__gt=0).count(),
        'waiting': open_qs.filter(status__in=(sm.WAITING_OPERATOR, sm.ACTIVE), unread_for_operator__gt=0).count(),
        'offline': open_qs.filter(status=sm.OFFLINE).count(),
        'mine': open_qs.filter(assigned_operator=request.user).count(),
    }
    return ok({'filter': name, 'conversations': rows, 'counts': counts, 'now': iso(timezone.now())})


def customer_card(c):
    card = {'registered': bool(c.user_id), 'name': c.display_name, 'phone': c.display_phone, 'source_path': c.source_path,
            'created_at': iso(c.created_at), 'phone_verified': bool(c.user_id or c.guest_phone_verified)}
    if c.user_id:
        user = c.user
        card.update({
            'approval': user.get_approval_status_display() if hasattr(user, 'get_approval_status_display') else '',
            'price_level': user.price_level, 'erp_code': user.erp_code or '', 'user_url': reverse('admin:accounts_customuser_change', args=[user.pk]),
        })
        try:
            from orders.models import Order
            orders = Order.objects.filter(user=user).order_by('-created_at')
            card['orders_count'] = orders.count()
            card['orders'] = [{'id': o.pk, 'total': int(o.total_price or 0), 'status': o.get_status_display(), 'at': iso(o.created_at)}
                              for o in orders[:3]]
        except Exception:  # noqa: BLE001 - کارت مشتری هیچ‌وقت پیشخوان را نمی‌شکند
            logger.exception('خواندن سفارش‌های مشتری برای کارت گفتگو ناموفق بود.')
    return card


def actions_for(c, user):
    """ اقدام‌های مجاز برای کارشناس در وضعیت فعلی (از ماتریس) """
    rules = set(sm.allowed_rules(c.status, sm.OPERATOR))
    actions = []
    if 'T3' in rules:
        actions.append('claim')
    if 'T6' in rules:
        actions.append('waiting')
    if 'T15' in rules:
        actions.append('release')
    if 'T16' in rules:
        actions.append('reassign')
    if 'T17' in rules:
        actions.append('close')
    if 'T18' in rules:
        actions.append('reopen')
    return actions


def _get(conversation_id):
    try:
        return Conversation.objects.select_related('user', 'assigned_operator').get(pk=int(conversation_id))
    except (Conversation.DoesNotExist, ValueError):
        raise Http404


@require_GET
@never_cache
@operator_required
def detail_api(request, conversation_id):
    c = _get(conversation_id)
    items = conv.messages_after(c, request.GET.get('after', 0), include_internal=True)
    return ok({
        'conversation': {**conversation_row(c), 'last_seq': c.last_message_seq, 'last_read_seq': c.last_read_seq_by_operator,
                         'customer_last_read_seq': c.last_read_seq_by_customer},
        'messages': [message_for_operator(m) for m in items], 'card': customer_card(c), 'actions': actions_for(c, request.user),
        'typing': bool(load_cfg().chat_typing_indicator_enabled) and c.status != sm.CLOSED and chatcache.is_typing(c.pk, 'c'),
        'blocked': blocks.block_for_conversation(c).exists(),
    })


@require_POST
@operator_required
def reply_api(request, conversation_id):
    c = _get(conversation_id)
    payload = read_payload(request)
    try:
        client_msg_id = parse_uuid(payload.get('client_msg_id'))
        if client_msg_id is None:
            raise conv.ChatError('bad_request', 'درخواست نامعتبر است.')
        note = str(payload.get('note') or '').lower() in ('1', 'true', 'on', 'yes')
        files = [] if note else request.FILES.getlist('files')
        body = conv.clean_body(payload.get('body'), 4000, allow_empty=bool(files))
        uploads = attachments.prepare_uploads(files, load_cfg())
        message, created = conv.post_message(c, sender=ChatMessage.SENDER_OPERATOR, body=body, client_msg_id=client_msg_id,
                                             operator=request.user, internal=note, uploads=uploads)
        if created and payload.get('quick_reply_id'):
            QuickReply.objects.filter(pk=payload.get('quick_reply_id')).update(usage_count=F('usage_count') + 1)
    except conv.ChatError as error:
        return fail(error.code, error.message, error.status)
    except sm.InvalidTransition as error:
        return fail('invalid_transition', str(error), 409)
    except sm.TransitionConflict as error:
        return fail('conflict', str(error), 409)
    c = _get(conversation_id)
    return ok({'message': message_for_operator(message), 'created': created, 'conversation': conversation_row(c),
               'actions': actions_for(c, request.user)}, status=201 if created else 200)


@require_POST
@operator_required
def read_api(request, conversation_id):
    c = _get(conversation_id)
    updated = conv.mark_read(c, 'operator', read_payload(request).get('upto_seq'))
    return ok({'unread': updated.unread_for_operator, 'last_read_seq': updated.last_read_seq_by_operator})


ACTION_RULES = {'claim': 'T3', 'waiting': 'T6', 'release': 'T15', 'reassign': 'T16', 'close': 'T17', 'reopen': 'T18'}


@require_POST
@operator_required
def action_api(request, conversation_id):
    c = _get(conversation_id)
    payload = read_payload(request)
    action = str(payload.get('action') or '')
    rule_id = ACTION_RULES.get(action)
    if rule_id is None:
        return fail('bad_request', 'اقدام نامعتبر است.')
    new_operator = None
    if action == 'reassign':
        User = get_user_model()
        target = User.objects.filter(pk=payload.get('operator_id') or 0).first()
        if target is None or not is_operator(target):
            return fail('bad_request', 'کارشناس مقصد معتبر نیست.')
        new_operator = target
    try:
        updated = conv.apply_rule(c, rule_id, sm.OPERATOR, operator=request.user, new_operator=new_operator)
    except sm.InvalidTransition as error:
        return fail('invalid_transition', str(error), 409)
    except sm.TransitionConflict as error:
        return fail('conflict', str(error), 409)
    return ok({'conversation': conversation_row(updated), 'actions': actions_for(updated, request.user)})


@require_GET
@never_cache
@operator_required
def quick_replies_api(request):
    qs = QuickReply.objects.filter(is_active=True).filter(Q(owner__isnull=True) | Q(owner=request.user)).order_by('sort_order', 'title')
    return ok({'items': [{'id': q.pk, 'title': q.title, 'body': q.body, 'shortcut': q.shortcut, 'personal': q.owner_id is not None}
                         for q in qs]})


@require_GET
@never_cache
@operator_required
def operators_api(request):
    User = get_user_model()
    users = User.objects.filter(is_active=True, is_staff=True).filter(Q(is_superuser=True) | Q(groups__permissions__codename='operate_chat') |
                                                                    Q(user_permissions__codename='operate_chat')).distinct()
    return ok({'items': [{'id': u.pk, 'name': operator_name(u)} for u in users[:50]]})


# ------------------------------------------------------------------ حضور و نشانگر نوشتن

def presence_state(user, cfg=None, now=None):
    cfg = cfg or load_cfg()
    now = now or timezone.now()
    row = presence.OperatorPresence.objects.filter(operator=user).first()
    return {'online': bool(row and row.is_online), 'online_count': presence.online_count(cfg, now),
            'timeout': int(cfg.chat_operator_timeout_seconds)}


@require_http_methods(['GET', 'POST'])
@never_cache
@operator_required
def presence_api(request):
    """
    GET: وضعیت من و تعداد کارشناسان آنلاین. POST بدون بدنه (یا ping): فقط نبض؛ POST با online=true/false: کلید دستی آنلاین/آفلاین.
    نبض کلید را روشن نمی‌کند (کارشناسی که خودش آفلاین شده با نبض خودکار صفحه دوباره آنلاین نمی‌شود).
    """
    if request.method == 'POST':
        payload = read_payload(request)
        online = payload.get('online')
        if online is not None:
            online = str(online).lower() in ('1', 'true', 'on', 'yes')
        presence.heartbeat(request.user, online=online)
    return ok(presence_state(request.user))


@require_POST
@operator_required
def typing_api(request, conversation_id):
    c = _get(conversation_id)
    cfg = load_cfg()
    if c.status != sm.CLOSED and cfg.chat_typing_indicator_enabled:
        count = chatcache.incr_window(f'rl:typing:op:{c.pk}', 60)
        if count is None or count <= 40:
            chatcache.mark_typing(c.pk, 'o')
    return ok({})


# ------------------------------------------------------------------ پیوست و مسدودسازی

@require_GET
@operator_required
def file_api(request, file_id):
    attachment = ChatAttachment.objects.filter(public_id=file_id).first()
    response = attachments.file_response(attachment) if attachment else None
    if response is None:
        raise Http404
    return response


@require_POST
@operator_required
def block_api(request, conversation_id):
    """ مسدود/آزاد کردن صاحب گفتگو. action=block (reason، hours اختیاری؛ خالی = دائمی، close=true گفتگوی باز را هم می‌بندد) یا unblock """
    c = _get(conversation_id)
    payload = read_payload(request)
    action = str(payload.get('action') or 'block')
    if action == 'unblock':
        blocks.unblock(c)
        return ok({'blocked': False})
    if action != 'block':
        return fail('bad_request', 'اقدام نامعتبر است.')
    try:
        hours = int(payload.get('hours') or 0) or None
    except (TypeError, ValueError):
        return fail('bad_request', 'مدت نامعتبر است.')
    if hours is not None and not 1 <= hours <= 24 * 365:
        return fail('bad_request', 'مدت نامعتبر است.')
    blocks.block(c, request.user, str(payload.get('reason') or '').strip(), hours)
    if str(payload.get('close') or '').lower() in ('1', 'true', 'on', 'yes') and c.status != sm.CLOSED:
        try:
            conv.apply_rule(c, 'T17', sm.OPERATOR, operator=request.user, meta={'reason': 'blocked'})
        except (sm.InvalidTransition, sm.TransitionConflict):
            pass
    c = _get(conversation_id)
    return ok({'blocked': True, 'conversation': conversation_row(c), 'actions': actions_for(c, request.user)})
