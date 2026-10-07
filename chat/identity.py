"""
هویت و دسترسی (IDOR): مالکیت گفتگو فقط با (۱) کاربر واردشده‌ی مالک، یا (۲) کوکی بازدیدکننده‌ی HttpOnly که هشِ آن روی گفتگو ذخیره است.
راز هرگز در URL نیست؛ public_id فقط آدرس است. شماره‌ی موبایل هرگز معیار مالکیت نیست. هر شکست دسترسی = همان ۴۰۴ «نیست».
"""
import hashlib
import hmac
import secrets
import uuid

from django.http import Http404

from .models import Conversation

VISITOR_COOKIE = 'chat_vid'
VISITOR_COOKIE_MAX_AGE = 365 * 24 * 3600


def hash_token(token):
    return hashlib.sha256((token or '').encode('utf-8')).hexdigest()


def new_visitor_token():
    return secrets.token_urlsafe(32)


def visitor_token(request):
    token = request.COOKIES.get(VISITOR_COOKIE, '')
    return token if 20 <= len(token) <= 100 else ''


def visitor_hash(request):
    token = visitor_token(request)
    return hash_token(token) if token else ''


def set_visitor_cookie(response, request, token):
    response.set_cookie(VISITOR_COOKIE, token, max_age=VISITOR_COOKIE_MAX_AGE, httponly=True, samesite='Lax',
                        secure=request.is_secure())


def can_access(request, conversation):
    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated and conversation.user_id == user.pk:
        return True
    current = visitor_hash(request)
    return bool(current and conversation.visitor_hash and hmac.compare_digest(conversation.visitor_hash, current))


def get_conversation_or_404(request, public_id):
    """ گفتگو با public_id اگر درخواست مالک آن است؛ وگرنه Http404 یکسان (نبودن و مال دیگری بودن فرقی ندارند) """
    try:
        key = uuid.UUID(str(public_id))
    except ValueError:
        raise Http404
    conversation = Conversation.objects.filter(public_id=key).select_related('user').first()
    if conversation is None or not can_access(request, conversation):
        raise Http404
    return conversation


def find_conversation_for_request(request):
    """ جدیدترین گفتگوی این بازدیدکننده/کاربر (باز یا بسته) یا None """
    from django.db.models import Q

    query = Q()
    user = getattr(request, 'user', None)
    if user is not None and user.is_authenticated:
        query |= Q(user=user)
    current = visitor_hash(request)
    if current:
        query |= Q(visitor_hash=current, user__isnull=True)
    if not query:
        return None
    return Conversation.objects.filter(query).select_related('user').order_by('-id').first()
