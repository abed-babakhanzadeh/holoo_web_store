"""
تسک دوره‌ای چت (Celery Beat، هر ۳۰ ثانیه؛ زمان‌بندی در config/celery.py). خودِ تسک سبک است: اگر چیزی سررسید نشده باشد فقط یک
کوئری ایندکس‌دار (next_timer_at) اجرا می‌شود. قفل کوتاه Redis جلوی هم‌پوشانی جاروها را می‌گیرد (ایمنی اصلی با CAS است نه این قفل).
"""
import logging

from celery import shared_task

from . import cache as chatcache

logger = logging.getLogger(__name__)


@shared_task(name='chat.tasks.sweep_chat_timers', ignore_result=True)
def sweep_chat_timers():
    from .timers import release_absent_assignees, run_due_timers

    got = chatcache.acquire('sweep:lock', 25)
    if got is False:
        return {'skipped': 'locked'}
    result = {}
    try:
        fired = run_due_timers()
        released = release_absent_assignees()
        result = {'fired': fired, 'released': released}
        if fired or released:
            logger.info('جاروی چت: %s', result)
    finally:
        if got:
            chatcache.forget('sweep:lock')
    return result


@shared_task(name='chat.tasks.purge_expired_chats', ignore_result=True)
def purge_expired_chats():
    """ شبانه: گفتگوهای بسته‌ی قدیمی‌تر از chat_retention_days را پاک می‌کند (۰ = غیرفعال) """
    from .retention import purge_expired

    return purge_expired()
