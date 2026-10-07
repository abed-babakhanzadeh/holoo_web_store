"""
نگهداری و پاک‌سازی دوره‌ای: گفتگوهای **بسته‌شده**ای که از chat_retention_days روز پیش بسته شده‌اند با همه‌ی پیام‌ها، رخدادها و پیوست‌هایشان
حذف می‌شوند (فایل‌ها با django_cleanup از دیسک پاک می‌شوند). ۰ = برای همیشه نگه‌دار (پیش‌فرض). گفتگوی باز هرگز پاک نمی‌شود.
مسدودسازی‌ها بعد از پاک‌شدن گفتگوی مبدأ می‌مانند (فقط ارجاع به گفتگو خالی می‌شود). دسته‌ای (۱۰۰تایی) تا قفل طولانی نگیرد.
"""
import logging
from datetime import timedelta

from django.utils import timezone

from . import statemachine as sm
from .models import Conversation
from .settingsio import cfg as load_cfg

logger = logging.getLogger(__name__)

BATCH = 100
MAX_BATCHES = 50


def purge_expired(now=None):
    """ ← تعداد گفتگوهای حذف‌شده """
    now = now or timezone.now()
    days = int(load_cfg().chat_retention_days or 0)
    if days <= 0:
        return 0
    cutoff = now - timedelta(days=days)
    deleted = 0
    for _ in range(MAX_BATCHES):
        ids = list(Conversation.objects.filter(status=sm.CLOSED, closed_at__lt=cutoff).order_by('closed_at')
                   .values_list('pk', flat=True)[:BATCH])
        if not ids:
            break
        try:
            Conversation.objects.filter(pk__in=ids).delete()
        except Exception:  # noqa: BLE001 - دسته‌ی خراب جارو را متوقف نمی‌کند؛ بار بعد دوباره
            logger.exception('پاک‌سازی دسته‌ی گفتگوها ناموفق بود.')
            break
        deleted += len(ids)
        if len(ids) < BATCH:
            break
    if deleted:
        logger.info('پاک‌سازی چت: %s گفتگوی منقضی حذف شد.', deleted)
    return deleted
