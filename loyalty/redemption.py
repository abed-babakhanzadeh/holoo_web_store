"""
ارکستریتور تبدیل امتیاز باشگاه مشتریان به شارژ کیف‌پول (Loyalty Phase 4A).

هسته‌ی هر دو دفترکل (loyalty/services.py::debit_points و wallet/services.py::credit_wallet)
عمداً کاملاً دست‌نخورده می‌مانند؛ این فایل فقط آن دو را ارکستره می‌کند - دقیقاً هم‌الگوی جداسازی
loyalty/earning.py و loyalty/cancellation.py که به‌جای تزریق منطق به services.py، یک ماژول
مجزا برای هر نگرانی هستند.

ترتیب قفل ثابت و اجباری در کل پروژه: همیشه ابتدا LoyaltyAccount (از طریق debit_points)، سپس
Wallet (از طریق credit_wallet) - هرگز برعکس. این ترتیب، احتمال بن‌بست (Deadlock) بین این دو
دفترکل را ساختاری به صفر می‌رساند؛ هیچ کد دیگری در پروژه نباید این دو را با ترتیب معکوس قفل کند.

نکته‌ی کلیدی معماری: قفل ردیفیِ LoyaltyAccount که debit_points با select_for_update می‌گیرد،
یک Savepoint داخلی است، نه یک تراکنش مستقل - پس تا پایان همین تراکنش دیتابیسیِ بیرونی (که این
تابع با db_transaction.atomic باز می‌کند) باز می‌ماند. یعنی از لحظه‌ای که debit_points برمی‌گردد
تا پایان تابع، هیچ فراخوانی هم‌زمان دیگری با همین idempotency_key نمی‌تواند در همین بازه اجرا
شود - این دقیقاً همان تضمینی است که چک تکراربودنِ WalletTransaction (که خودِ credit_wallet
هیچ ایدمپوتنسی‌ای برایش ندارد) را Race-Free می‌کند.
"""

from django.db import transaction as db_transaction
from django.db.models import Sum
from django.utils import timezone

from products.models import SiteSettings
from wallet.models import Wallet, WalletTransaction
from wallet.services import credit_wallet

from . import services as loyalty_services
from .exceptions import IdempotencyKeyConflictError
from .models import LoyaltyTransaction

WALLET_REFERENCE_TYPE = 'loyalty_transaction'


class RedemptionValidationError(ValueError):
    """ درخواست تبدیل امتیاز، مستقل از وضعیت لحظه‌ای دیتابیس، نامعتبر است (کف/سقف/موجودی/سقف روزانه). """


def _daily_redeemed_points(account, *, today_start):
    total = LoyaltyTransaction.objects.filter(
        account=account, transaction_type=LoyaltyTransaction.REDEEM_WALLET, created_at__gte=today_start,
    ).aggregate(total=Sum('amount'))['total'] or 0
    return -total   # amount در دفترکل منفی ذخیره می‌شود؛ سقف روزانه بر مبنای مقدار مثبت سنجیده می‌شود


def redeem_points_to_wallet(user, points, *, idempotency_key):
    """
    تبدیل اتمیک `points` امتیاز باشگاه کاربر به شارژ کیف‌پول او؛ مطابق نرخ/سقف‌های تنظیمات سایت.

    idempotency_key: فراخوانی مکرر با همان کلید (همان کاربر، همان points) همیشه دقیقاً همان
    جفت (LoyaltyTransaction, WalletTransaction) را بدون کسر/شارژ دوباره برمی‌گرداند. همان کلید
    با points متفاوت یا برای کاربر دیگر => IdempotencyKeyConflictError.

    خروجی: (LoyaltyTransaction, WalletTransaction)
    """
    settings_obj = SiteSettings.cached()
    points = int(points)

    if points < settings_obj.loyalty_redeem_min_points:
        raise RedemptionValidationError(
            f'حداقل {settings_obj.loyalty_redeem_min_points} امتیاز برای تبدیل لازم است.'
        )
    if points > settings_obj.loyalty_redeem_max_points_per_transaction:
        raise RedemptionValidationError(
            f'حداکثر {settings_obj.loyalty_redeem_max_points_per_transaction} امتیاز در هر تراکنش مجاز است.'
        )

    with db_transaction.atomic():
        # گام ۱: کسر از لجر امتیاز - قفل ردیفی LoyaltyAccount اینجا گرفته می‌شود و تا پایان همین
        # تراکنش بیرونی باز می‌ماند (نگاه کنید توضیح بالای فایل).
        loyalty_txn = loyalty_services.debit_points(
            user, points, LoyaltyTransaction.REDEEM_WALLET,
            'تبدیل امتیاز به کیف‌پول', idempotency_key=idempotency_key,
        )

        # گام ۲: محافظت اضافی روی مالکیت - debit_points فقط amount/transaction_type را برای
        # تشخیص تعارض کلید چک می‌کند، نه صاحب حساب. بدون این چک، سوءاستفاده از کلید یک کاربر
        # دیگر (با همان points تصادفی) می‌توانست کیف‌پول کاربر فعلی را بدون کسر واقعی از حساب او شارژ کند.
        if loyalty_txn.account.user_id != user.id:
            raise IdempotencyKeyConflictError(
                f'کلید ضدتکرار «{idempotency_key}» متعلق به کاربر دیگری است.'
            )

        # گام ۳: تشخیص بازپخش (Replay) - به‌لطف قفل هنوز بازِ LoyaltyAccount، این کوئری از این‌جا
        # به بعد Race-Free است؛ هیچ فراخوانی هم‌زمان دیگری با همین کلید نمی‌تواند همزمان اینجا باشد.
        existing_wallet_txn = WalletTransaction.objects.filter(
            reference_type=WALLET_REFERENCE_TYPE, reference_id=loyalty_txn.pk,
        ).first()
        if existing_wallet_txn is not None:
            return loyalty_txn, existing_wallet_txn

        # گام ۴: سقف روزانه (تایم‌زون محلی) - فقط برای درخواست واقعاً تازه بررسی می‌شود؛ یک
        # Replay هرگز به‌خاطر سقفی که بعداً پر شده رد نمی‌شود. عبور از سقف => رول‌بک کامل شامل کسر گام ۱.
        today_start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
        today_redeemed = _daily_redeemed_points(loyalty_txn.account, today_start=today_start)
        if today_redeemed > settings_obj.loyalty_redeem_max_points_per_day:
            raise RedemptionValidationError(
                f'سقف روزانه‌ی تبدیل امتیاز ({settings_obj.loyalty_redeem_max_points_per_day}) پر شده است.'
            )

        # گام ۵: شارژ کیف‌پول - فقط برای درخواست تازه، فقط یک‌بار.
        toman_amount = points * settings_obj.loyalty_redeem_toman_per_point
        wallet, _ = Wallet.objects.get_or_create(user=user)
        wallet_txn = credit_wallet(
            wallet, toman_amount, WalletTransaction.KIND_LOYALTY_REDEEM,
            reference_type=WALLET_REFERENCE_TYPE, reference_id=loyalty_txn.pk,
            description=f'تبدیل {points} امتیاز باشگاه مشتریان',
        )

    return loyalty_txn, wallet_txn
