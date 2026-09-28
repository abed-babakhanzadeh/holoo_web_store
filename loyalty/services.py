"""
تنها لایه‌ی مجاز برای تغییر LoyaltyAccount.current_balance/lifetime_earned/lifetime_redeemed و
افزودن LoyaltyTransaction. هیچ کد دیگری (ادمین، ویو، شل، سیگنال) نباید مستقیم این فیلدها را
بنویسد یا LoyaltyTransaction.objects.create() را صدا بزند.

Phase 1 عمداً هیچ اتصال خودکاری ندارد (نه به پرداخت سفارش، نه به مرجوعی، نه به Tier جاری در
accounts.models.CustomUser) - فقط دو عملیات هسته‌ای اتمیک: credit_points/debit_points. اتصال
خودکار (سیگنال پرداخت موفق، سیگنال‌های returns، بازمحاسبه‌ی Tier و ...) موضوع فازهای بعدی است.

الگوی قفل/اتمیک دقیقاً هم‌شکل wallet/services.py (credit_wallet/debit_wallet): select_for_update
داخل transaction.atomic، و برای debit، بررسی کافی‌بودن موجودی *داخل* همان قفل - تا هیچ درخواست
هم‌زمان دیگری نتواند از همان لحظه‌ی رقابتی سوءاستفاده کند (Double-Spend).
"""

from django.db import IntegrityError
from django.db import transaction as db_transaction

from .exceptions import IdempotencyKeyConflictError, InsufficientPointsError
from .models import LoyaltyAccount, LoyaltyTransaction

# برای رقابت هم‌زمان روی یک idempotency_key یکسان: یک بار تلاش اصلی + یک بار بازخوانی رکورد برنده
_IDEMPOTENCY_ATTEMPTS = 2


def _lock_account(account):
    """ قفل ردیفی LoyaltyAccount؛ باید داخل transaction.atomic صدا زده شود. بدون LIMIT (سازگار با SQL Server). """
    return LoyaltyAccount.objects.select_for_update().get(pk=account.pk)


def _validate_amount(amount):
    amount = int(amount)
    if amount <= 0:
        raise ValueError('مقدار امتیاز باید عددی مثبت باشد.')
    return amount


def _validate_reason(reason):
    reason = (reason or '').strip()
    if not reason:
        raise ValueError('علت تراکنش الزامی است.')
    return reason


def _existing_idempotent_transaction(idempotency_key, expected_amount, transaction_type):
    """
    اگر تراکنشی با این کلید از قبل ثبت شده باشد، همان رکورد برگردانده می‌شود (رفتار ایدمپوتنت) -
    اما فقط اگر amount/transaction_type رکورد قبلی دقیقاً با درخواست فعلی یکسان باشد. اگر همان
    کلید با amount یا transaction_type متفاوتی دوباره فرستاده شود (یعنی کلید به‌اشتباه برای دو
    رویداد متفاوت استفاده شده)، رکورد قبلی بی‌سروصدا برگردانده نمی‌شود - IdempotencyKeyConflictError
    صادر می‌شود تا این ناسازگاری هرگز بی‌صدا قورت داده نشود.
    """
    existing = LoyaltyTransaction.objects.filter(idempotency_key=idempotency_key).first()
    if existing is None:
        return None
    if existing.amount != expected_amount or existing.transaction_type != transaction_type:
        raise IdempotencyKeyConflictError(
            f'کلید ضدتکرار «{idempotency_key}» قبلاً با پارامترهای متفاوتی ثبت شده '
            f'(ثبت‌شده: amount={existing.amount}, type={existing.transaction_type}؛ '
            f'درخواست فعلی: amount={expected_amount}, type={transaction_type}).'
        )
    return existing


def credit_points(user, amount, transaction_type, reason, *, source_type='', source_id=None,
                   idempotency_key=None, expires_at=None, created_by=None):
    """
    افزایش اتمیک موجودی امتیاز (کسب از خرید/فعالیت، اعطای دستی، برگشت). amount باید مثبت باشد؛
    روی دفترکل به همان مثبتی و با remaining_amount=amount (برای کسر FIFO در فازهای بعدی) ثبت می‌شود.

    idempotency_key: اگر تراکنشی با همین کلید از قبل ثبت شده باشد، همان رکورد بدون اعمال دوباره
    برگردانده می‌شود (رفتار ایدمپوتنت، نه خطا) - برای رویدادهای سیستمی که ممکن است دوباره تحویل
    داده شوند (مثلاً تلاش دوباره‌ی یک سیگنال/تسک در فازهای بعدی).
    """
    amount = _validate_amount(amount)
    reason = _validate_reason(reason)

    for attempt in range(1, _IDEMPOTENCY_ATTEMPTS + 1):
        try:
            with db_transaction.atomic():
                account = LoyaltyAccount.get_or_create_for_user(user)
                locked = _lock_account(account)

                # چک ایدمپوتنسی عمداً *بعد* از گرفتن قفل ردیفی است، نه قبل از آن: اگر پیش از
                # قفل بود، دو نخ هم‌زمان با همان idempotency_key هر دو می‌توانستند این چک را رد
                # کنند (چون نخ برنده هنوز commit نکرده)، بعد پشتِ قفل صف بکشند - نخ بازنده که
                # قفل را بعداً می‌گیرد دیگر این چک را دوباره نمی‌دید و مستقیم به نوشتن می‌رفت.
                # با قرارگیری *داخل* قفل، نخ بازنده بعد از آزادسازی قفل بلافاصله رکورد برنده را
                # می‌بیند و idempotent برمی‌گردد - قبل از اینکه دوباره چیزی بنویسد.
                if idempotency_key:
                    existing = _existing_idempotent_transaction(idempotency_key, amount, transaction_type)
                    if existing is not None:
                        return existing

                locked.current_balance += amount
                locked.lifetime_earned += amount
                locked.save(update_fields=['current_balance', 'lifetime_earned', 'updated_at'])

                return LoyaltyTransaction.objects.create(
                    account=locked, amount=amount, balance_after=locked.current_balance,
                    transaction_type=transaction_type, source_type=source_type or '', source_id=source_id,
                    idempotency_key=idempotency_key, expires_at=expires_at, remaining_amount=amount,
                    reason=reason, created_by=created_by,
                )
        except IntegrityError:
            # فقط برای رقابت هم‌زمان روی همان idempotency_key قابل‌بازیابی است؛ transaction.atomic
            # کل این تلاش (شامل افزایش current_balance/lifetime_earned) را خودکار rollback کرده -
            # هیچ افزایش دوباره‌ای ثبت نشده. تلاش بعدی رکورد ثبت‌شده توسط نخ برنده را پیدا می‌کند.
            if not idempotency_key or attempt == _IDEMPOTENCY_ATTEMPTS:
                raise
    raise AssertionError('unreachable')  # حلقه‌ی بالا همیشه یا return می‌کند یا raise


def debit_points(user, amount, transaction_type, reason, *, source_type='', source_id=None,
                  idempotency_key=None, created_by=None):
    """
    کاهش اتمیک موجودی امتیاز (خرج/کسر دستی/برگشت). بررسی کافی‌بودن current_balance عمداً *داخل*
    قفل select_for_update انجام می‌شود (نه فقط در فرم/ادمین) تا هیچ درخواست هم‌زمان دیگری نتواند
    از همین لحظه‌ی رقابتی سوءاستفاده کند.

    نکته‌ی عمدی درباره‌ی transaction_type=REVERSE: در Phase 1 هیچ رفتار ویژه‌ای برای REVERSE
    لحاظ نشده - از نظر این تابع REVERSE دقیقاً مثل هر debit دیگری عمل می‌کند (lifetime_redeemed
    بالا می‌رود، نه lifetime_earned پایین). تأثیر واقعی مرجوعی/لغو روی Tier/سطح مشتری و روی
    lifetime_earned، تصمیمی است که عمداً به فاز ۲ (Earn/Reverse Engine) موکول شده - نگاه کنید
    گزارش ممیزی فاز صفر.
    """
    amount = _validate_amount(amount)
    reason = _validate_reason(reason)

    for attempt in range(1, _IDEMPOTENCY_ATTEMPTS + 1):
        try:
            with db_transaction.atomic():
                account = LoyaltyAccount.get_or_create_for_user(user)
                locked = _lock_account(account)

                # هم‌دلیل credit_points بالا: چک ایدمپوتنسی *داخل* قفل، نه قبل از آن - وگرنه یک
                # نخ بازنده که موجودی را (بعد از کسر نخ برنده) ناکافی می‌بیند، به‌جای برگرداندن
                # رکورد موجود، با InsufficientPointsError متوقف می‌شد؛ دقیقاً همان چیزی که تست
                # هم‌زمانی loyalty/tests_cancellation.py::CancellationConcurrencyTests کشف کرد.
                if idempotency_key:
                    existing = _existing_idempotent_transaction(idempotency_key, -amount, transaction_type)
                    if existing is not None:
                        return existing

                if locked.current_balance < amount:
                    raise InsufficientPointsError(
                        f'موجودی امتیاز کافی نیست (موجودی فعلی: {locked.current_balance}، درخواستی: {amount}).'
                    )
                locked.current_balance -= amount
                locked.lifetime_redeemed += amount
                locked.save(update_fields=['current_balance', 'lifetime_redeemed', 'updated_at'])

                return LoyaltyTransaction.objects.create(
                    account=locked, amount=-amount, balance_after=locked.current_balance,
                    transaction_type=transaction_type, source_type=source_type or '', source_id=source_id,
                    idempotency_key=idempotency_key, remaining_amount=0,
                    reason=reason, created_by=created_by,
                )
        except IntegrityError:
            if not idempotency_key or attempt == _IDEMPOTENCY_ATTEMPTS:
                raise
    raise AssertionError('unreachable')
