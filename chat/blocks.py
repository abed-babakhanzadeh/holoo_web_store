"""
مسدودسازی بازدیدکننده‌ی مزاحم (ChatBlock). معیار: کوکی بازدیدکننده (visitor_hash) و/یا حساب کاربری؛ IP عمداً نه. مسدودشده
نمی‌تواند گفتگو بسازد، پیام بفرستد یا پیوست بفرستد؛ تاریخچه‌ی خودش را می‌خواند. محدودیت شناخته‌شده: مهمانی که کوکی را پاک کند
مسدودیتِ کوکی را دور می‌زند؛ برای کاربر ثبت‌نام‌شده مسدودیت روی حساب است و با پاک‌کردن کوکی از بین نمی‌رود.
"""
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from . import identity
from .models import ChatBlock


def _active(now=None):
    now = now or timezone.now()
    return ChatBlock.objects.filter(is_active=True).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))


def is_blocked_identity(visitor_hash='', user=None, now=None):
    query = Q()
    if visitor_hash:
        query |= Q(visitor_hash=visitor_hash)
    if user is not None and getattr(user, 'is_authenticated', False):
        query |= Q(user=user)
    if not query:
        return False
    return _active(now).filter(query).exists()


def is_blocked(request, now=None):
    return is_blocked_identity(identity.visitor_hash(request), getattr(request, 'user', None), now)


def block_for_conversation(conversation):
    """ مسدودیت‌های فعالِ مربوط به هویت این گفتگو (کوکی یا کاربر) """
    query = Q()
    if conversation.visitor_hash:
        query |= Q(visitor_hash=conversation.visitor_hash)
    if conversation.user_id:
        query |= Q(user_id=conversation.user_id)
    if not query:
        return ChatBlock.objects.none()
    return _active().filter(query)


def block(conversation, by, reason='', hours=None, now=None):
    """ هویت صاحب گفتگو را مسدود می‌کند. hours=None ← دائمی. ← ردیف ChatBlock """
    now = now or timezone.now()
    existing = block_for_conversation(conversation).first()
    expires = now + timedelta(hours=int(hours)) if hours else None
    if existing is not None:
        existing.expires_at = expires
        existing.reason = (reason or existing.reason)[:200]
        existing.save(update_fields=['expires_at', 'reason'])
        return existing
    return ChatBlock.objects.create(
        visitor_hash=conversation.visitor_hash or '', user=conversation.user if conversation.user_id else None, conversation=conversation,
        reason=(reason or '')[:200], blocked_by=by, expires_at=expires)


def unblock(conversation):
    return block_for_conversation(conversation).update(is_active=False)
