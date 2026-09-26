"""
تنها لایه‌ی مجاز برای تغییر balance/reserved_balance و افزودن WalletTransaction.

هیچ کد دیگری (ادمین، ویو، شل، تسک) نباید مستقیم wallet.balance را بنویسد یا
WalletTransaction.objects.create() را صدا بزند - همه‌ی مسیرها باید از این توابع رد شوند تا
قفل ردیفی، اتمیک‌بودن و invariant های مالی (بدون منفی‌شدن، بدون Double Spending) در یک‌جا
تضمین شوند. الگوی قفل/اتمیک دقیقاً هم‌شکل accounts/address.py::_lock_user_rows است.
"""

from decimal import Decimal

from django.db import transaction as db_transaction
from django.utils import timezone

from notifications.service import notify_admin

from .models import Wallet, WalletTransaction, WithdrawalRequest


class InsufficientBalanceError(Exception):
    """ موجودی قابل‌استفاده برای این عملیات کافی نیست. """


class InvalidWithdrawalStateError(Exception):
    """ درخواست برداشت در وضعیتی نیست که این انتقال روی آن مجاز باشد (قبلاً تصمیم‌گیری شده). """


def _lock_wallet(wallet):
    """ قفل ردیفی wallet؛ باید داخل transaction.atomic صدا زده شود. بدون LIMIT (سازگار با SQL Server). """
    return Wallet.objects.select_for_update().get(pk=wallet.pk)


def credit_wallet(wallet, amount, kind, *, reference_type='', reference_id=None, description='', created_by=None):
    """
    افزایش موجودی (شارژ، مرجوعی فاکتور، تبدیل امتیاز وفاداری، ...).
    amount باید مثبت باشد؛ روی دفترکل به‌همان مثبتی ثبت می‌شود.
    """
    amount = Decimal(amount)
    if amount <= 0:
        raise ValueError('مبلغ واریز باید مثبت باشد.')

    with db_transaction.atomic():
        locked = _lock_wallet(wallet)
        locked.balance += amount
        locked.save(update_fields=['balance', 'updated_at'])
        txn = WalletTransaction.objects.create(
            wallet=locked, amount=amount, kind=kind, balance_after=locked.balance,
            reference_type=reference_type, reference_id=reference_id,
            description=description, created_by=created_by,
        )
    return txn


def debit_wallet(wallet, amount, kind, *, reference_type='', reference_id=None, description='', created_by=None):
    """
    کسر موجودی (مثلاً پرداخت سبد خرید با کیف پول در فازهای بعدی). بر مبنای available_balance
    چک می‌شود، نه balance خام - تا مبلغِ بلوکه‌شده برای برداشت هرگز دوباره خرج نشود.
    """
    amount = Decimal(amount)
    if amount <= 0:
        raise ValueError('مبلغ برداشت باید مثبت باشد.')

    with db_transaction.atomic():
        locked = _lock_wallet(wallet)
        if locked.available_balance < amount:
            raise InsufficientBalanceError('موجودی قابل‌استفاده کافی نیست.')
        locked.balance -= amount
        locked.save(update_fields=['balance', 'updated_at'])
        txn = WalletTransaction.objects.create(
            wallet=locked, amount=-amount, kind=kind, balance_after=locked.balance,
            reference_type=reference_type, reference_id=reference_id,
            description=description, created_by=created_by,
        )
    return txn


def reserve_withdrawal(wallet, amount, *, account_holder, card_number='', iban='', description=''):
    """
    مسدودسازی مبلغ در reserved_balance و صدور WithdrawalRequest؛ balance هنوز کم نمی‌شود
    (فقط با complete_withdrawal کم می‌شود) تا اگر ادمین رد کرد، بازگرداندن ساده باشد.
    بعد از commit، پیامک اطلاع به مدیر شلیک می‌شود (notify_admin هرگز استثنا نمی‌دهد و
    مسیر اصلی ثبت درخواست را نمی‌شکند - نگاه کنید notifications/service.py).
    """
    amount = Decimal(amount)
    if amount <= 0:
        raise ValueError('مبلغ درخواستی باید مثبت باشد.')
    if not (account_holder or '').strip():
        raise ValueError('نام صاحب حساب الزامی است.')

    with db_transaction.atomic():
        locked = _lock_wallet(wallet)
        if locked.available_balance < amount:
            raise InsufficientBalanceError('موجودی قابل‌استفاده کافی نیست.')
        locked.reserved_balance += amount
        locked.save(update_fields=['reserved_balance', 'updated_at'])

        request = WithdrawalRequest.objects.create(
            wallet=locked, amount=amount, account_holder_snapshot=account_holder,
            card_number_snapshot=card_number, iban_snapshot=iban,
        )

        phone = locked.user.phone_number if locked.user_id else '-'
        db_transaction.on_commit(lambda: notify_admin(
            'withdrawal_requested_admin',
            phone=phone, amount=int(amount), card=card_number or '-', iban=iban or '-',
        ))
    return request


def complete_withdrawal(withdrawal_request, admin_user):
    """
    تسویه‌ی نهایی (بعد از واریز دستیِ ادمین به شبا/کارت واقعی): کسر هم‌زمان از balance
    و reserved_balance، ثبت یک WalletTransaction برداشت، و بستن درخواست با status=COMPLETED.
    """
    with db_transaction.atomic():
        locked_request = (
            WithdrawalRequest.objects.select_for_update().select_related('wallet').get(pk=withdrawal_request.pk)
        )
        if locked_request.status != WithdrawalRequest.STATUS_PENDING:
            raise InvalidWithdrawalStateError('این درخواست قبلاً تصمیم‌گیری شده است.')

        locked_wallet = _lock_wallet(locked_request.wallet)
        locked_wallet.balance -= locked_request.amount
        locked_wallet.reserved_balance -= locked_request.amount
        locked_wallet.save(update_fields=['balance', 'reserved_balance', 'updated_at'])

        txn = WalletTransaction.objects.create(
            wallet=locked_wallet, amount=-locked_request.amount, kind=WalletTransaction.KIND_WITHDRAWAL,
            balance_after=locked_wallet.balance, reference_type='withdrawal_request',
            reference_id=locked_request.pk, created_by=admin_user,
        )

        locked_request.status = WithdrawalRequest.STATUS_COMPLETED
        locked_request.transaction = txn
        locked_request.decided_at = timezone.now()
        locked_request.decided_by = admin_user
        locked_request.save(update_fields=['status', 'transaction', 'decided_at', 'decided_by'])
    return locked_request


def reject_withdrawal(withdrawal_request, reason, admin_user):
    """ رد درخواست: فقط reserved_balance آزاد می‌شود؛ balance اصلاً دست نمی‌خورد (پول جایی نرفته بود). """
    if not (reason or '').strip():
        raise ValueError('دلیل رد الزامی است.')

    with db_transaction.atomic():
        locked_request = (
            WithdrawalRequest.objects.select_for_update().select_related('wallet').get(pk=withdrawal_request.pk)
        )
        if locked_request.status != WithdrawalRequest.STATUS_PENDING:
            raise InvalidWithdrawalStateError('این درخواست قبلاً تصمیم‌گیری شده است.')

        locked_wallet = _lock_wallet(locked_request.wallet)
        locked_wallet.reserved_balance -= locked_request.amount
        locked_wallet.save(update_fields=['reserved_balance', 'updated_at'])

        locked_request.status = WithdrawalRequest.STATUS_REJECTED
        locked_request.rejection_reason = reason
        locked_request.decided_at = timezone.now()
        locked_request.decided_by = admin_user
        locked_request.save(update_fields=['status', 'rejection_reason', 'decided_at', 'decided_by'])
    return locked_request


def reverse_transaction(original_txn, *, reason, created_by=None):
    """
    ثبت یک تراکنش معکوس (اصلاحی) برای original_txn؛ خودِ original_txn طبق قرارداد Immutable
    هرگز ویرایش/حذف نمی‌شود (WalletTransaction.save/delete این را در سطح مدل هم رد می‌کنند).
    """
    if not (reason or '').strip():
        raise ValueError('دلیل تراکنش معکوس الزامی است.')

    with db_transaction.atomic():
        locked = _lock_wallet(original_txn.wallet)
        reversed_amount = -original_txn.amount
        locked.balance += reversed_amount
        locked.save(update_fields=['balance', 'updated_at'])
        txn = WalletTransaction.objects.create(
            wallet=locked, amount=reversed_amount, kind=WalletTransaction.KIND_REVERSAL,
            balance_after=locked.balance, reference_type='wallet_transaction',
            reference_id=original_txn.pk, description=reason, created_by=created_by,
        )
    return txn
