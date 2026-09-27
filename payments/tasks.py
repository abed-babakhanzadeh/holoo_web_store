"""
تسک دوره‌ای انقضای پرداخت‌های ترکیبی معلق (Wallet Phase 4).

اگر کاربر بعد از کسر سهم کیف‌پول اصلاً به درگاه برنگردد (تب را بست، مرورگر کرش کرد، ...)،
Transaction تا ابد در وضعیت 'pending' و سهم کیف‌پول تا ابد کسرشده می‌ماند. این تسک دوره‌ای
تور ایمنی است - همان الگوی notifications.tasks.retry_pending_notifications و
promotions.tasks.release_expired_coupon_reservations.
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.db import transaction as db_transaction
from django.utils import timezone

from .checkout import _reverse_wallet_leg_locked
from .models import Transaction

logger = logging.getLogger(__name__)

STALE_PENDING_AFTER = timedelta(minutes=30)


@shared_task
def expire_stale_pending_wallet_transactions():
    """
    Finder سبک و بدون قفل: فقط شناسه‌های کاندید را می‌خواند و هرکدام را به تسک جدا می‌سپارد
    (هم‌سبک retry_pending_notifications) - تا خودِ این تسک هیچ‌وقت select_for_update نگیرد.
    """
    cutoff = timezone.now() - STALE_PENDING_AFTER
    stale_ids = list(
        Transaction.objects.filter(status='pending', wallet_amount__gt=0, created_at__lt=cutoff)
        .values_list('id', flat=True)[:500]
    )
    for txn_id in stale_ids:
        expire_single_pending_transaction.delay(txn_id)

    if stale_ids:
        logger.info("انقضای درگاه: %s تراکنش pending منقضی به صف رفت.", len(stale_ids))
    return f"queued={len(stale_ids)}"


@shared_task
def expire_single_pending_transaction(txn_id):
    """
    Worker per-row: تغییر status و بازگشت وجه کیف‌پول در *یک* اتمیک با *یک* قفل - تا پنجره‌ی
    ناامن «status=failed ولی هنوز Reverse نشده» هرگز به‌صورت نیمه‌کاره commit نشود. گارد
    status != 'pending' دقیقاً همان چیزی است که PaymentCallbackView هم دارد؛ اگر کاربر درست
    همین لحظه از درگاه برگردد، هرکدام زودتر قفل ردیف را بگیرد برنده است و دیگری بی‌خطر خارج
    می‌شود - بدون نیاز به هیچ قفل/هماهنگی سطح بالاتر.
    """
    with db_transaction.atomic():
        locked = Transaction.objects.select_for_update().get(pk=txn_id)
        if locked.status != 'pending':
            return "already processed"
        locked.status = 'failed'
        locked.save(update_fields=['status', 'updated_at'])
        _reverse_wallet_leg_locked(locked, reason='انقضای درگاه (۳۰ دقیقه بدون بازگشت)')
    return "expired"
