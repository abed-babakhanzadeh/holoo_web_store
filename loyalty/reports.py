"""
گزارش مالی و حسابرسی تعهدات باشگاه مشتریان برای پنل ادمین (Loyalty Phase 5D-2).

همه‌چیز با کوئری‌های تجمیعی ثابت (Sum/Coalesce) ساخته می‌شود؛ هیچ رکورد لجر در حافظه بار
نمی‌شود - دقیقاً هم‌فلسفه‌ی promotions/reports.py. طبق ممیزی فاز ۵D-1: چهار متریک اصلی روی
LoyaltyAccount (سبک‌تر، یک ردیف به‌ازای هر کاربر دارای حساب) و تفکیک کانال بازخرید منحصراً روی
LoyaltyTransaction (چون transaction_type فقط آن‌جا وجود دارد).

ارزش ریالی تعهدات یک برآورد لحظه‌ای بر مبنای نرخ *فعلی* تبدیل است، نه ارزش‌گذاری تاریخی دقیق -
دقیقاً همان چیزی که مصوبه‌ی ۵D-1 خواسته.
"""

from django.db.models import Sum
from django.db.models.functions import Coalesce

from products.models import SiteSettings

from .models import LoyaltyAccount, LoyaltyTransaction

_ZERO = 0

CHANNEL_LABELS = (
    (LoyaltyTransaction.REDEEM_WALLET, 'تبدیل به کیف‌پول'),
    (LoyaltyTransaction.REDEEM_REWARD, 'بازخرید پاداش/کوپن'),
    (LoyaltyTransaction.ADMIN_DEBIT, 'کسر دستی ادمین'),
)


def _sum(field, **kwargs):
    return Coalesce(Sum(field, **kwargs), _ZERO)


def build_financial_report():
    """
    دیکشنری ساده (قابل رندر و قابل تست) با کوئری‌های تجمیعیِ ثابت - مستقل از حجم داده.

    خروجی:
      outstanding_points     : جمع current_balance همه‌ی حساب‌ها (کل امتیازات در گردش)
      lifetime_earned        : جمع lifetime_earned همه‌ی حساب‌ها (کل کسب‌شده در طول عمر سیستم)
      lifetime_redeemed      : جمع lifetime_redeemed همه‌ی حساب‌ها (کل مصرف‌شده در طول عمر سیستم)
      burn_to_earn_ratio     : lifetime_redeemed/lifetime_earned × 100 (صفر اگر lifetime_earned صفر باشد)
      toman_per_point        : نرخ تبدیل فعلی (SiteSettings.loyalty_redeem_toman_per_point)
      points_liability_toman : outstanding_points × toman_per_point (برآورد لحظه‌ای، نه تاریخی)
      channels                : فهرست دیکشنری‌های {key, label, amount} برای هر کانال کسر - amount همیشه مثبت
    """
    accounts_row = LoyaltyAccount.objects.aggregate(
        outstanding_points=_sum('current_balance'),
        lifetime_earned=_sum('lifetime_earned'),
        lifetime_redeemed=_sum('lifetime_redeemed'),
    )

    lifetime_earned = accounts_row['lifetime_earned']
    lifetime_redeemed = accounts_row['lifetime_redeemed']
    burn_to_earn_ratio = round(lifetime_redeemed * 100 / lifetime_earned, 1) if lifetime_earned else 0

    toman_per_point = SiteSettings.cached().loyalty_redeem_toman_per_point
    points_liability_toman = accounts_row['outstanding_points'] * toman_per_point

    # amount برای انواع کسر منفی ذخیره می‌شود (قرارداد مدل)؛ اینجا با علامت مثبت گزارش می‌شود
    debit_rows = {
        row['transaction_type']: -row['total']
        for row in LoyaltyTransaction.objects.filter(
            transaction_type__in=[key for key, _label in CHANNEL_LABELS],
        ).values('transaction_type').annotate(total=_sum('amount'))
    }
    channels = [
        {'key': key, 'label': label, 'amount': debit_rows.get(key, 0)}
        for key, label in CHANNEL_LABELS
    ]

    return {
        'outstanding_points': accounts_row['outstanding_points'],
        'lifetime_earned': lifetime_earned,
        'lifetime_redeemed': lifetime_redeemed,
        'burn_to_earn_ratio': burn_to_earn_ratio,
        'toman_per_point': toman_per_point,
        'points_liability_toman': points_liability_toman,
        'channels': channels,
    }
