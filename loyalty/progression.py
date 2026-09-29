"""
پایش ارتقای رتبه‌ی داینامیک هم‌زمان با کسب امتیاز (Loyalty Phase 5C-2).

فقط برای مسیرهای *کسب* معنا دارد - چون get_dynamic_tier_for_user/get_tier_for_lifetime_points
منحصراً روی LoyaltyAccount.lifetime_earned کار می‌کنند و این فیلد هرگز با debit/redeem کم
نمی‌شود (نگاه کنید گزارش ممیزی فاز ۵C-1)؛ یعنی فقط credit_points می‌تواند رتبه را عوض کند -
هیچ مسیر بازخرید (loyalty/redemption.py، loyalty/reward_redemption.py) هرگز این تابع را صدا
نمی‌زند و نیازی هم ندارد.

هیچ‌کدام از loyalty/services.py، loyalty/redemption.py، loyalty/reward_redemption.py تغییر
نکردند - این فایل فقط credit_points موجود را ارکستره می‌کند (هم‌الگوی دقیق جداسازیِ
loyalty/redemption.py و loyalty/reward_redemption.py از هسته‌ی سرویس).

قفل ردیفی: خودِ این تابع پیش از خواندن سطح «قبل»، قفل LoyaltyAccount را می‌گیرد (بدون دستکاری
loyalty/services.py) تا رقابت هم‌زمان بین «خواندن سطح قبل» و «کسب امتیاز» ممکن نشود - دقیقاً
همان رفع Double-Debit Race که در loyalty/reward_redemption.py (فاز ۴B) اعمال شد. قفل دوباره‌ی
همان ردیف توسط credit_points (چند خط پایین‌تر، در همان تراکنش/اتصال) کاملاً بی‌خطر و Re-entrant
است.

ایزولاسیون اعلان (خط قرمز صریح این فاز): ثبت LoyaltyTierHistory و فراخوانی notify() در یک
try/except جدا از منطق کسب امتیاز قرار دارند؛ هر خطای غیرمنتظره در این بخش لاگ می‌شود و هرگز به
بیرون نشت نمی‌کند - تراکنش کسب امتیاز (که پیش از رسیدن به این بخش کامل انجام شده) هرگز به‌خاطر
شکست تاریخچه/پیامک رول‌بک نمی‌شود.
"""

import logging

from django.db import transaction as db_transaction
from django.utils import timezone

from notifications.service import notify

from . import services
from .models import LoyaltyAccount, LoyaltyTierHistory

logger = logging.getLogger(__name__)


def credit_points_with_progression(user, amount, transaction_type, reason, **kwargs):
    """
    عیناً services.credit_points را صدا می‌زند و اگر lifetime_earned از آستانه‌ی یک LoyaltyTier
    تازه عبور کرد، یک ردیف LoyaltyTierHistory ثبت و پیامک اطلاع می‌فرستد. خروجی همیشه دقیقاً
    همان LoyaltyTransaction ی است که credit_points برمی‌گرداند - هیچ رفتار مالی‌ای عوض نمی‌شود.
    """
    with db_transaction.atomic():
        account = LoyaltyAccount.get_or_create_for_user(user)
        locked = LoyaltyAccount.objects.select_for_update().get(pk=account.pk)
        before_tier = services.get_tier_for_lifetime_points(locked.lifetime_earned)

        loyalty_txn = services.credit_points(user, amount, transaction_type, reason, **kwargs)

        after_tier = services.get_tier_for_lifetime_points(loyalty_txn.account.lifetime_earned)
        if after_tier is not None and after_tier != before_tier:
            try:
                history = LoyaltyTierHistory.objects.create(
                    account=loyalty_txn.account, old_tier=before_tier, new_tier=after_tier,
                    triggering_transaction=loyalty_txn,
                )
                notify(user.phone_number, 'loyalty_tier_upgraded_customer', tier_title=after_tier.title)
                history.notified_at = timezone.now()
                history.save(update_fields=['notified_at'])
            except Exception:
                logger.exception(
                    'ثبت تاریخچه/اعلان ارتقای رتبه برای کاربر %s (تراکنش #%s) ناموفق بود؛ '
                    'خودِ کسب امتیاز دست‌نخورده و معتبر می‌ماند.', user.id, loyalty_txn.pk,
                )

    return loyalty_txn
