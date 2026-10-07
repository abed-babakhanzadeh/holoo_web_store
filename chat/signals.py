"""
اتصال گفتگوهای مهمان به حساب بعد از ورود (فقط با کوکی بازدیدکننده‌ی همین مرورگر).

قواعد (طرح مهندسی Rev2): شماره‌ی موبایل هرگز معیار مالکیت نیست. فقط گفتگوهایی که visitor_hash آن‌ها با کوکی این مرورگر یکی است و
هنوز مالک ندارند و جدیدتر از GUEST_ATTACH_DAYS روزند به کاربر وصل می‌شوند. اگر موبایلِ واردشده‌ی مهمان با موبایل حساب فرق داشت، فقط یک
پرچم در رخداد ثبت می‌شود تا کارشناس ببیند (اتصال باز هم طبق کوکی است).
"""
import logging
from datetime import timedelta

from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver
from django.utils import timezone

from . import identity
from .conversations import GUEST_ATTACH_DAYS
from .models import ChatEvent, Conversation

logger = logging.getLogger(__name__)


@receiver(user_logged_in, dispatch_uid='chat_attach_guest_conversations')
def attach_guest_conversations(sender, request, user, **kwargs):
    try:
        visitor = identity.visitor_hash(request) if request is not None else ''
        if not visitor:
            return
        since = timezone.now() - timedelta(days=GUEST_ATTACH_DAYS)
        pending = list(Conversation.objects.filter(visitor_hash=visitor, user__isnull=True, created_at__gte=since))
        for conversation in pending:
            mismatch = bool(conversation.guest_phone and conversation.guest_phone != user.phone_number)
            updated = Conversation.objects.filter(pk=conversation.pk, user__isnull=True).update(user=user)
            if updated:
                ChatEvent.objects.create(conversation=conversation, type='attached', actor_type='system', actor_id=user.pk,
                                         from_status=conversation.status, to_status=conversation.status,
                                         meta={'phone_mismatch': mismatch})
    except Exception:  # noqa: BLE001 - ورود کاربر هرگز نباید به‌خاطر چت بشکند
        logger.exception('اتصال گفتگوهای مهمان به حساب ناموفق بود.')
