"""
اتصال payments به رویدادهای اپ‌های دیگر (Wallet Phase 4).

فقط در این اپ (نه در wallet/) چون payments از قبل به orders.Order وابسته است (FK مستقیم
Transaction.order)؛ اضافه‌کردن یک @receiver روی سیگنال orders هیچ وابستگی جدیدی نمی‌سازد.
wallet/ همچنان هیچ‌چیزی از orders/payments نمی‌داند - جهت وابستگی یک‌طرفه دست‌نخورده می‌ماند.
"""

import logging

from django.dispatch import receiver

from orders.signals import order_canceled

from .checkout import reverse_wallet_leg
from .models import Transaction

logger = logging.getLogger(__name__)


@receiver(order_canceled, dispatch_uid='payments_reverse_wallet_on_cancel')
def on_order_canceled(sender, order, **kwargs):
    """
    اگر سفارش لغوشده سهم کیف‌پولِ برگشت‌نخورده دارد (Wallet-only یا Mixed، در هر وضعیتی از
    Transaction)، آن سهم برمی‌گردد. با .filter نه .first: یک سفارش می‌تواند بیش از یک
    Transaction با wallet_amount>0 داشته باشد (مثلاً یک تلاش Mixed شکست‌خورده که خودش قبلاً
    Reverse شده، به‌همراه یک تلاش موفق بعدی) - هرکدام مستقل و Idempotent پردازش می‌شود.

    خطای یک ردیف نباید پردازش بقیه‌ی ردیف‌های همین سفارش را متوقف کند (send_robust هم برای
    خودِ این رسیور همین تضمین را نسبت به بقیه‌ی شنونده‌های order_canceled می‌دهد، اما داخل
    خودِ این رسیور هم باید همان تضمین برای چند Transaction برقرار باشد).
    """
    candidate_ids = list(
        Transaction.objects.filter(
            order=order, wallet_amount__gt=0, wallet_reversed_at__isnull=True,
        ).values_list('id', flat=True)
    )
    for txn_id in candidate_ids:
        try:
            reverse_wallet_leg(txn_id, reason=f'لغو سفارش #{order.id}')
        except Exception:
            logger.exception(
                "بازگشت سهم کیف‌پول تراکنش #%s (سفارش #%s) پس از لغو سفارش ناموفق بود؛ "
                "بررسی دستی لازم است.", txn_id, order.id,
            )
