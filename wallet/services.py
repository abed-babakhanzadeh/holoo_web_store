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

from notifications.service import notify, notify_admin

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


def _customer_name(wallet):
    """ نام نمایشی کاربر برای متن پیامک؛ بدون نام یعنی شماره موبایل """
    user = wallet.user
    return (user.first_name or user.phone_number) if user else '-'


def reserve_withdrawal(wallet, amount, *, account_holder, card_number='', iban='', description=''):
    """
    مسدودسازی مبلغ در reserved_balance و صدور WithdrawalRequest (status=PENDING)؛ balance هنوز
    کم نمی‌شود (فقط با mark_withdrawal_paid کم می‌شود) تا اگر ادمین رد کرد، بازگرداندن ساده باشد.
    چک کافی‌بودن available_balance عمداً *داخل* قفل select_for_update انجام می‌شود (نه فقط در
    فرم/UI) تا هیچ درخواست هم‌زمان دیگری نتواند از همین لحظه‌ی رقابتی سوءاستفاده کند.
    بعد از commit، دو پیامک شلیک می‌شود (مشتری + مدیر)؛ notify()/notify_admin() هرگز استثنا
    نمی‌دهند و مسیر اصلی ثبت درخواست را نمی‌شکنند - نگاه کنید notifications/service.py.
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
        name = _customer_name(locked)
        db_transaction.on_commit(lambda: notify(
            phone, 'withdrawal_requested_customer', name=name, amount=int(amount),
        ))
        db_transaction.on_commit(lambda: notify_admin(
            'withdrawal_requested_admin',
            phone=phone, amount=int(amount), card=card_number or '-', iban=iban or '-',
        ))
    return request


def approve_withdrawal(withdrawal_request, admin_user):
    """
    تأیید اولیه (بعد از بررسی مدارک): فقط از PENDING مجاز است. balance/reserved_balance و لجر
    هیچ‌کدام دست نمی‌خورند - پول همچنان فقط بلوکه است، هنوز واقعاً واریز نشده. فقط status/
    decided_at/decided_by ثبت و پیامک تأیید به مشتری شلیک می‌شود.
    """
    with db_transaction.atomic():
        locked_request = WithdrawalRequest.objects.select_for_update().select_related('wallet__user').get(pk=withdrawal_request.pk)
        if locked_request.status != WithdrawalRequest.STATUS_PENDING:
            raise InvalidWithdrawalStateError('این درخواست در وضعیت «در انتظار بررسی» نیست.')

        locked_request.status = WithdrawalRequest.STATUS_APPROVED
        locked_request.decided_at = timezone.now()
        locked_request.decided_by = admin_user
        locked_request.save(update_fields=['status', 'decided_at', 'decided_by'])

        phone = locked_request.wallet.user.phone_number if locked_request.wallet.user_id else '-'
        name = _customer_name(locked_request.wallet)
        db_transaction.on_commit(lambda: notify(
            phone, 'withdrawal_approved_customer', name=name, amount=int(locked_request.amount),
        ))
    return locked_request


def mark_withdrawal_paid(withdrawal_request, admin_user):
    """
    تسویه‌ی نهایی (بعد از واریز دستیِ واقعیِ ادمین به شبا/کارت): فقط و فقط از APPROVED مجاز
    است - نه مستقیم از PENDING. اینجا کسر قطعی از balance و reserved_balance هم‌زمان انجام
    می‌شود، یک WalletTransaction برداشت ثبت و paid_at/status=COMPLETED ذخیره می‌شود.
    """
    with db_transaction.atomic():
        locked_request = WithdrawalRequest.objects.select_for_update().select_related('wallet__user').get(pk=withdrawal_request.pk)
        if locked_request.status != WithdrawalRequest.STATUS_APPROVED:
            raise InvalidWithdrawalStateError('این درخواست ابتدا باید تأیید شود (وضعیت APPROVED).')

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
        locked_request.paid_at = timezone.now()
        locked_request.save(update_fields=['status', 'transaction', 'paid_at'])

        phone = locked_request.wallet.user.phone_number if locked_request.wallet.user_id else '-'
        name = _customer_name(locked_request.wallet)
        db_transaction.on_commit(lambda: notify(
            phone, 'withdrawal_paid_customer', name=name, amount=int(locked_request.amount),
        ))
    return locked_request


def reject_withdrawal(withdrawal_request, reason, admin_user):
    """
    رد درخواست: هم از PENDING هم از APPROVED مجاز است (مثلاً بعد از تأیید معلوم شد شبا غلط
    بوده). فقط reserved_balance آزاد می‌شود؛ balance اصلاً دست نمی‌خورد چون پول جایی نرفته بود.
    """
    if not (reason or '').strip():
        raise ValueError('دلیل رد الزامی است.')

    with db_transaction.atomic():
        locked_request = WithdrawalRequest.objects.select_for_update().select_related('wallet__user').get(pk=withdrawal_request.pk)
        if locked_request.status not in (WithdrawalRequest.STATUS_PENDING, WithdrawalRequest.STATUS_APPROVED):
            raise InvalidWithdrawalStateError('این درخواست قبلاً تصمیم‌گیری نهایی شده است.')

        locked_wallet = _lock_wallet(locked_request.wallet)
        locked_wallet.reserved_balance -= locked_request.amount
        locked_wallet.save(update_fields=['reserved_balance', 'updated_at'])

        locked_request.status = WithdrawalRequest.STATUS_REJECTED
        locked_request.rejection_reason = reason
        locked_request.decided_at = timezone.now()
        locked_request.decided_by = admin_user
        locked_request.save(update_fields=['status', 'rejection_reason', 'decided_at', 'decided_by'])

        phone = locked_request.wallet.user.phone_number if locked_request.wallet.user_id else '-'
        name = _customer_name(locked_request.wallet)
        db_transaction.on_commit(lambda: notify(
            phone, 'withdrawal_rejected_customer', name=name, amount=int(locked_request.amount), reason=reason,
        ))
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
