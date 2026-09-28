""" استثناهای دامنه‌ی باشگاه مشتریان. """


class InsufficientPointsError(Exception):
    """ موجودی امتیاز کاربر برای این عملیات کافی نیست. """


class LedgerImmutableError(Exception):
    """ تلاش برای ویرایش یا حذف مستقیم یک رکورد دفترکل امتیاز (LoyaltyTransaction). """


class IdempotencyKeyConflictError(Exception):
    """
    همان idempotency_key قبلاً با amount/transaction_type متفاوتی ثبت شده. یعنی یا کلید به‌اشتباه
    برای دو رویداد متفاوت استفاده شده، یا مقدار رویداد بین دو تلاش تغییر کرده - در هر دو حالت
    بازگرداندن رکورد قبلی بدون اطلاع، اشتباه است؛ باید صریحاً خطا داد.
    """


class LoyaltyTierDeletionError(Exception):
    """ تلاش برای حذف فیزیکی یک سطح باشگاه مشتریان (LoyaltyTier) - فقط غیرفعال‌سازی مجاز است. """


class RewardInactiveError(Exception):
    """ پاداش یا کوپن پشتِ آن غیرفعال/در بازه‌ی معتبر نیست (Loyalty Phase 4B). """


class RewardAlreadyRedeemedError(Exception):
    """ این کاربر قبلاً همین پاداش را بازخرید کرده - الگوی Master Coupon فقط یک‌بار در عمر مجاز است. """


class RewardOutOfStockError(Exception):
    """ ظرفیت کوپنِ پشتِ این پاداش (claim_limit یا total_limit) پر شده است. """
