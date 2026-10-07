"""
API عمومی گفتگو (مشتری/مهمان): همه JSON با قالب خطای ثابت {ok:false, code, message}. CSRF با هدر X-CSRFToken. دسترسی فقط با
chat/identity.py (کاربر مالک یا کوکی بازدیدکننده)؛ هر شکست ۴۰۴ یکسان. پولینگ با after=<seq> و مرجع دیتابیس.
"""
import json
import logging
import uuid
from datetime import timedelta

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from accounts.models import normalize_phone_number
from accounts.throttle import get_client_ip
from products.chat_settings import current_hours_state, viewer_allowed
from .settingsio import cfg as load_cfg

from . import cache as chatcache
from . import conversations as conv
from . import identity
from . import statemachine as sm
from .models import ChatMessage, Conversation
from .serializers import conversation_for_customer, message_for_customer
from .availability import live_available
from .services import availability
from .text import safe_inline

logger = logging.getLogger(__name__)

MIN_FILL_MS = 1500
TYPING_PER_MINUTE = 40                                # کلاینت هر ~۳ ثانیه یک‌بار می‌فرستد؛ بیشتر از این نادیده گرفته می‌شود


def fail(code, message, status=400, **extra):
    return JsonResponse({'ok': False, 'code': code, 'message': message, **extra}, status=status,
                        json_dumps_params={'ensure_ascii': False})


def ok(payload=None, status=200):
    return JsonResponse({'ok': True, **(payload or {})}, status=status, json_dumps_params={'ensure_ascii': False})


def read_payload(request):
    """ بدنه‌ی JSON یا فرم؛ هرگز استثنا نمی‌اندازد """
    if (request.content_type or '').startswith('application/json'):
        try:
            data = json.loads(request.body.decode('utf-8') or '{}')
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}
    return request.POST.dict()


def parse_uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def widget_allowed(request, cfg, *, need_tab='offline'):
    """ چت روشن، این نوع بازدیدکننده مجاز، و زبانه‌ی لازم فعال باشد """
    if not cfg.chat_enabled or not viewer_allowed(cfg, request.user.is_authenticated):
        return False
    return bool(getattr(cfg, f'chat_tab_{need_tab}_enabled', False))


def ip_trunc(ip):
    if ':' in ip:                                     # IPv6: چهار بخش اول
        return ':'.join(ip.split(':')[:4])[:45]
    parts = ip.split('.')
    return '.'.join(parts[:3] + ['0']) if len(parts) == 4 else ip[:45]


# ------------------------------------------------------------------ محدودیت نرخ (Redis، با fallback دیتابیس)

def check_message_rate(request, cfg, ident):
    limit = int(cfg.chat_rate_limit_per_minute)
    count = chatcache.incr_window(f'rl:msg:{ident}', 60)
    if count is None:                                  # Redis قطع: شمارش از DB
        since = timezone.now() - timedelta(seconds=60)
        vh = identity.visitor_hash(request)
        query = ChatMessage.objects.filter(sender_type=ChatMessage.SENDER_CUSTOMER, created_at__gte=since)
        if request.user.is_authenticated:
            query = query.filter(conversation__user=request.user)
        elif vh:
            query = query.filter(conversation__visitor_hash=vh)
        else:
            return
        count = query.count() + 1
    if count > limit:
        raise conv.ChatError('rate_limited', 'پیام‌های شما خیلی سریع ارسال می‌شود؛ کمی صبر کنید.', 429)


def check_new_conversation_cap(cfg, ip):
    cap = int(cfg.chat_guest_max_conversations_per_day)
    day = timezone.now().strftime('%Y%m%d')
    count = chatcache.incr_window(f'rl:conv:{ip}:{day}', 86400)
    if count is None:
        start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        count = Conversation.objects.filter(client_ip_trunc=ip_trunc(ip), created_at__gte=start).count() + 1
    if count > cap:
        raise conv.ChatError('daily_cap', 'تعداد گفتگوهای امروز از این دستگاه به سقف رسیده است؛ فردا دوباره تلاش کنید.', 429)


def guest_fields(cfg, payload):
    """ نام و موبایل مهمان طبق حالت‌های تنظیمات (hidden/optional/required) """
    result = {}
    for field, mode in (('name', cfg.chat_guest_name_mode), ('phone', cfg.chat_guest_phone_mode)):
        value = str(payload.get(field) or '').strip()
        if mode == 'hidden':
            value = ''
        elif not value and mode == 'required':
            raise conv.ChatError('field_required', 'نام را وارد کنید.' if field == 'name' else 'شماره موبایل را وارد کنید.')
        if field == 'name':
            value = safe_inline(value, 80)
        elif value:
            try:
                value = normalize_phone_number(value)
            except ValueError as error:
                raise conv.ChatError('bad_phone', str(error))
        result[field] = value
    return result


def page_path(value):
    """ فقط مسیر پایه: بدون Query و Fragment و پارامترهای ردیابی (utm) """
    value = str(value or '').split('#', 1)[0].split('?', 1)[0].strip()
    return value[:300] if value.startswith('/') else ''


# ------------------------------------------------------------------ endpointها

@require_GET
@never_cache
def state_view(request):
    cfg = load_cfg()
    if not cfg.chat_enabled or not viewer_allowed(cfg, request.user.is_authenticated):
        return ok({'enabled': False})
    now = timezone.now()
    hours = current_hours_state(cfg, now)
    existing = identity.find_conversation_for_request(request)
    payload = {'enabled': True, 'availability': availability(hours, cfg, now), 'conversation': None}
    if existing is not None and existing.status != sm.CLOSED:
        payload['conversation'] = conversation_for_customer(existing)
        chatcache.mark_customer_seen(existing.pk)
    return ok(payload)


@require_POST
def create_view(request):
    cfg = load_cfg()
    payload = read_payload(request)
    channel = str(payload.get('channel') or 'offline')
    if channel not in ('offline', 'live'):
        return fail('bad_request', 'درخواست نامعتبر است.')
    if not widget_allowed(request, cfg, need_tab=channel):
        return fail('disabled', 'گفتگوی آنلاین در دسترس نیست.', 403)
    try:
        if channel == 'live' and not live_available(cfg):
            raise conv.ChatError('not_available', 'هم‌اکنون کارشناسی برای گفتگوی زنده در دسترس نیست؛ پیام آفلاین بگذارید.', 409)
        if str(payload.get('website') or '').strip():
            raise conv.ChatError('spam', 'درخواست پذیرفته نشد.')
        if not request.user.is_authenticated:
            try:
                elapsed = int(payload.get('elapsed_ms') or 0)
            except (TypeError, ValueError):
                elapsed = 0
            if elapsed < MIN_FILL_MS:
                raise conv.ChatError('too_fast', 'لطفاً دوباره تلاش کنید.')
        client_msg_id = parse_uuid(payload.get('client_msg_id'))
        if client_msg_id is None:
            raise conv.ChatError('bad_request', 'درخواست نامعتبر است.')
        body = conv.clean_body(payload.get('message') or payload.get('body'), int(cfg.chat_message_max_length))
        guest = {'name': '', 'phone': ''} if request.user.is_authenticated else guest_fields(cfg, payload)

        ip = get_client_ip(request)
        token = identity.visitor_token(request) or identity.new_visitor_token()
        vh = identity.hash_token(token)
        check_message_rate(request, cfg, vh)
        existing = identity.find_conversation_for_request(request)
        if existing is None or existing.status == sm.CLOSED:
            check_new_conversation_cap(cfg, ip)

        create = conv.create_live_conversation if channel == 'live' else conv.create_offline_conversation
        conversation, message = create(
            user=request.user if request.user.is_authenticated else None, visitor_hash=vh, name=guest['name'], phone=guest['phone'],
            body=body, client_msg_id=client_msg_id, source_path=page_path(payload.get('page_path')), ip=ip_trunc(ip))
    except conv.ChatError as error:
        return fail(error.code, error.message, error.status)
    except sm.TransitionConflict as error:
        return fail('conflict', str(error), 409)
    response = ok({'conversation': conversation_for_customer(conversation), 'message': message_for_customer(message)}, status=201)
    identity.set_visitor_cookie(response, request, token)
    return response


@require_GET
@never_cache
def messages_view(request, public_id):
    conversation = identity.get_conversation_or_404(request, public_id)
    chatcache.mark_customer_seen(conversation.pk)
    items = conv.messages_after(conversation, request.GET.get('after', 0), include_internal=False)
    typing = bool(load_cfg().chat_typing_indicator_enabled) and conversation.status != sm.CLOSED and chatcache.is_typing(conversation.pk, 'o')
    return ok({'conversation': conversation_for_customer(conversation), 'messages': [message_for_customer(m) for m in items],
               'typing': typing})


@require_POST
def send_view(request, public_id):
    conversation = identity.get_conversation_or_404(request, public_id)
    cfg = load_cfg()
    if not cfg.chat_enabled:
        return fail('disabled', 'گفتگوی آنلاین در دسترس نیست.', 403)
    payload = read_payload(request)
    try:
        client_msg_id = parse_uuid(payload.get('client_msg_id'))
        if client_msg_id is None:
            raise conv.ChatError('bad_request', 'درخواست نامعتبر است.')
        body = conv.clean_body(payload.get('body') or payload.get('message'), int(cfg.chat_message_max_length))
        check_message_rate(request, cfg, identity.visitor_hash(request) or f'u{request.user.pk}')
        message, created = conv.post_message(conversation, sender=ChatMessage.SENDER_CUSTOMER, body=body, client_msg_id=client_msg_id)
    except conv.ChatError as error:
        return fail(error.code, error.message, error.status)
    except sm.TransitionConflict as error:
        return fail('conflict', str(error), 409)
    conversation = Conversation.objects.get(pk=conversation.pk)
    return ok({'conversation': conversation_for_customer(conversation), 'message': message_for_customer(message), 'created': created},
              status=201 if created else 200)


@require_POST
def read_view(request, public_id):
    conversation = identity.get_conversation_or_404(request, public_id)
    updated = conv.mark_read(conversation, 'customer', read_payload(request).get('upto_seq'))
    return ok({'conversation': conversation_for_customer(updated)})


@require_POST
def close_view(request, public_id):
    conversation = identity.get_conversation_or_404(request, public_id)
    try:
        updated = conv.apply_rule(conversation, 'T17', sm.CUSTOMER)
    except sm.InvalidTransition:
        return fail('closed', 'این گفتگو قبلاً بسته شده است.', 409)
    except sm.TransitionConflict as error:
        return fail('conflict', str(error), 409)
    return ok({'conversation': conversation_for_customer(updated)})


@require_POST
def typing_view(request, public_id):
    """ «مشتری در حال نوشتن است»: فقط یک کلید کوتاه‌عمر در Redis (نه دیتابیس)؛ بدون Redis بی‌صدا نادیده گرفته می‌شود """
    conversation = identity.get_conversation_or_404(request, public_id)
    cfg = load_cfg()
    if conversation.status == sm.CLOSED or not cfg.chat_typing_indicator_enabled:
        return ok({'typing': False})
    count = chatcache.incr_window(f'rl:typing:{conversation.pk}', 60)
    if count is None or count <= TYPING_PER_MINUTE:
        chatcache.mark_typing(conversation.pk, 'c')
    return ok({'typing': True})
