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
