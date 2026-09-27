"""
هماهنگ‌کننده‌ی پرداخت سفارش (Wallet Phase 4).

این اپ (payments) Orchestrator است: تصمیم می‌گیرد پرداخت با کیف‌پول، درگاه یا هردو انجام
شود. wallet/ فقط سرویس‌های مالی خودش (debit_wallet/reverse_transaction) را ارائه می‌دهد و
هیچ‌وقت از orders/payments چیزی نمی‌داند - جهت وابستگی یک‌طرفه می‌ماند.

قرارداد حیاتی: تا وقتی «کل مبلغ سفارش» قطعی نشده (هم سهم کیف‌پول هم سهم درگاه)، هیچ
Transaction با status='success' ساخته نمی‌شود؛ Order.is_paid فقط وجود یک Transaction موفق را
چک می‌کند (نه جمع مبلغ‌ها)، پس این قرارداد در همین لایه تضمین می‌شود، نه در مدل.
"""

import logging
import secrets
from decimal import Decimal

from django.db import transaction as db_transaction
from django.utils import timezone

from wallet import services as wallet_services
from wallet.models import Wallet, WalletTransaction

from .models import Transaction

logger = logging.getLogger(__name__)


class InvalidWalletAmountError(ValueError):
    """ مقدار سهم کیف‌پول درخواستی نامعتبر است (منفی یا بیشتر از مبلغ سفارش). """


def _synthetic_wallet_authority():
    """ authority مصنوعی برای Wallet-only (بدون درگاه واقعی)؛ پیشوند WALLET هرگز با فرمت
    Axxxx تراکنش‌های درگاه واقعی برخورد نمی‌کند. secrets نه random: همان دلیل PaymentStartView -
    این رشته باید غیرقابل‌حدس باشد. """
    return f"WALLET{secrets.token_hex(16).upper()}"


def _generate_gateway_authority():
    return f"A{secrets.token_hex(16).upper()}"


def start_order_payment(order, user, wallet_amount_requested):
    """
    نقطه‌ی ورود واحد از ویو (PaymentStartView.post). سه حالت را تفکیک می‌کند و بازمی‌گرداند:
    (transaction, redirect_kind) - redirect_kind یکی از 'result' (پرداخت با کیف‌پول تمام شد) یا
    'gateway' (باید به mock_gateway هدایت شود، مثل جریان قبلی).

    فرض: order.can_pay از قبل در ویو چک شده.
    """
    wallet_amount_requested = Decimal(wallet_amount_requested or 0)
    if wallet_amount_requested < 0:
        raise InvalidWalletAmountError('سهم کیف‌پول نمی‌تواند منفی باشد.')
    remaining = order.total_price - wallet_amount_requested
    if remaining < 0:
        raise InvalidWalletAmountError('سهم کیف‌پول نمی‌تواند از مبلغ سفارش بیشتر باشد.')

    # تراکنشِ در انتظارِ قبلی همین سفارش (کاربر وسط راه برگشته)؛ دقیقاً همان دلیل PaymentStartView
    # فعلی - و این‌جا حیاتی‌تر است چون اگر یک تلاش Mixed از قبل سهم کیف‌پول را کسر کرده، تلاش
    # دوم با wallet_amount_requested متفاوت نباید دوباره کسر کند
    pending = Transaction.objects.filter(order=order, user=user, status='pending').first()
    if pending:
        return pending, 'gateway'

    if wallet_amount_requested == 0:
        return _start_gateway_only(order, user), 'gateway'

    # هر دو حالت Wallet-only و Mixed اول باید سهم کیف‌پول را واقعاً کسر کنند. debit_wallet خودش
    # select_for_update روی Wallet می‌گیرد (جلوگیری از Double Spending دو تب هم‌زمان) و
    # InsufficientBalanceError می‌دهد اگر available_balance زیر لحظه‌ی قفل کافی نباشد.
    with db_transaction.atomic():
        # get_or_create نه order.user.wallet: کیف‌پول به‌صورت Lazy ساخته می‌شود (هم‌سبک
        # wallet/views.py::_get_wallet) - کاربری که هرگز صفحه‌ی کیف‌پول را باز نکرده هم باید
        # بتواند اولین‌بار مستقیم از اینجا صاحب کیف‌پول (با موجودی صفر) شود
        wallet, _ = Wallet.objects.get_or_create(user=order.user)
        wallet_txn = wallet_services.debit_wallet(
            wallet, wallet_amount_requested, kind=WalletTransaction.KIND_CART_PAYMENT,
            reference_type='order', reference_id=order.id, created_by=user,
        )
        if remaining == 0:
            return _finalize_wallet_only(order, user, wallet_amount_requested, wallet_txn), 'result'
        return _start_mixed(order, user, wallet_amount_requested, remaining, wallet_txn), 'gateway'


def _start_gateway_only(order, user):
    """ همان منطق فعلی PaymentStartView.get، بدون تغییر """
    return Transaction.objects.create(
        user=user, order=order, amount=order.total_price, authority=_generate_gateway_authority(),
    )


def _finalize_wallet_only(order, user, wallet_amount, wallet_txn):
    from .views import PaymentCallbackView  # وارد کردن دیرهنگام: پرهیز از حلقه‌ی ایمپورت views<->checkout

    txn = Transaction.objects.create(
        user=user, order=order, amount=0, wallet_amount=wallet_amount,
        wallet_transaction=wallet_txn, authority=_synthetic_wallet_authority(),
        status='success', ref_id='کیف پول',
    )
    # فقط بعد از commit، وگرنه شنونده‌ای که سفارش/تراکنش را از دیتابیس می‌خواند با rollback مواجه می‌شود
    db_transaction.on_commit(lambda: PaymentCallbackView._on_payment_succeeded(order, txn))
    return txn


def _start_mixed(order, user, wallet_amount, remaining, wallet_txn):
    return Transaction.objects.create(
        user=user, order=order, amount=remaining, wallet_amount=wallet_amount,
        wallet_transaction=wallet_txn, authority=_generate_gateway_authority(), status='pending',
    )


# --------------------------------------------------------------- Reversal متمرکز (Wallet Phase 4)
def _reverse_wallet_leg_locked(locked_txn, *, reason, created_by=None):
    """
    هسته‌ی واقعی و *تنها* نقطه‌ی منطق Reversal کیف‌پول. هر ۳ مسیر (شکست/انصراف درگاه در
    PaymentCallbackView، تسک انقضای ۳۰ دقیقه‌ای در payments.tasks، رسیور order_canceled در
    payments.receivers) دقیقاً از این‌جا رد می‌شوند - مستقیم (وقتی از قبل قفل Transaction را
    دارند) یا از طریق reverse_wallet_leg پایین.

    فرض: locked_txn از قبل با select_for_update زیر یک db_transaction.atomic() باز، قفل شده -
    این تابع خودش قفل نمی‌گیرد (تا در بلاک اتمیک موجود caller بدون قفل تکراری/نستد ادغام شود).

    Idempotent: اگر چیزی برای برگرداندن نیست (wallet_amount=0) یا قبلاً برگشته
    (wallet_reversed_at ست شده)، بی‌صدا کاری نمی‌کند - این دقیقاً همان محافظتی است که جلوی شارژ
    مجدد کیف‌پول در اثر Callback تکراری/Race بین تسک انقضا و بازگشت واقعی کاربر را می‌گیرد.
    """
    if not locked_txn.wallet_amount or locked_txn.wallet_reversed_at:
        return False

    if locked_txn.wallet_transaction_id is None:
        # این حالت یعنی ناسازگاری داده (wallet_amount>0 بدون ردیف لجر متناظر) - نباید بی‌صدا رد
        # شود چون به‌معنای گم‌شدن ردی از پول کاربر است؛ باید صریح failآور شود تا در پنل/لاگ دیده شود
        logger.critical(
            "ناسازگاری داده: تراکنش #%s سهم کیف‌پول %s دارد اما wallet_transaction ندارد؛ "
            "بازگشت وجه انجام نشد و باید دستی بررسی شود.",
            locked_txn.pk, locked_txn.wallet_amount,
        )
        raise ValueError(
            f'تراکنش #{locked_txn.pk} سهم کیف‌پول دارد ولی به هیچ ردیف لجری وصل نیست؛ '
            'بازگشت وجه به‌صورت خودکار ممکن نیست.'
        )

    wallet_services.reverse_transaction(locked_txn.wallet_transaction, reason=reason, created_by=created_by)
    locked_txn.wallet_reversed_at = timezone.now()
    locked_txn.save(update_fields=['wallet_reversed_at'])
    return True


def reverse_wallet_leg(txn_id, *, reason, created_by=None):
    """
    Wrapper عمومی برای فراخوان‌هایی که از قبل قفل Transaction را ندارند (مثل رسیور
    order_canceled). خودش قفل می‌گیرد، بعد هسته‌ی بالا را صدا می‌زند.
    """
    with db_transaction.atomic():
        locked = Transaction.objects.select_for_update().get(pk=txn_id)
        return _reverse_wallet_leg_locked(locked, reason=reason, created_by=created_by)
