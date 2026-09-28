"""
ارکستریتور تبدیل امتیاز باشگاه مشتریان به کوپن/پاداش (Loyalty Phase 4B).

هسته‌ی هر دو دفترکل/مدل (loyalty/services.py::debit_points و promotions.models.Coupon/UserCoupon)
عمداً کاملاً دست‌نخورده می‌مانند؛ این فایل فقط آن‌ها را ارکستره می‌کند - هم‌الگوی loyalty/redemption.py
(فاز ۴A). الگوی صدور کوپن دقیقاً بازتولیدِ promotions/wallet.py::claim است (Master/Template
Coupon؛ مصوبه‌ی صریح فاز ۴B) - بدون تولید کد تخفیف تازه به‌ازای هر بازخرید.

ترتیب قفل ثابت و اجباری: همیشه ابتدا LoyaltyAccount، سپس Coupon - هرگز برعکس (تعمیم همان قانونی
که فاز ۴A برای LoyaltyAccount/Wallet وضع کرد).

نکته‌ی کلیدیِ رفع Double-Debit Race (مصوبه‌ی بازبینی طراحی): برخلاف فاز ۴A، اینجا خودِ ارکستریتور
(نه فقط debit_points) باید پیش از هر بررسی‌ای قفل ردیفی LoyaltyAccount را با select_for_update
بگیرد - چون الگوی Master Coupon ذاتاً «یک‌بار در عمر» است (UserCoupon.UniqueConstraint(coupon,
user))، و اگر بررسیِ «قبلاً بازخرید شده یا نه» پیش از این قفل انجام شود، دو درخواست هم‌زمان با
idempotency_key متفاوت هر دو از آن بررسی عبور می‌کنند و هر دو یک کسر واقعی و مستقل از لجر انجام
می‌دهند (دو کسر برای یک کوپن). با گرفتن قفل ردیفی LoyaltyAccount در همان ابتدا (پیش از
debit_points)، نخ دوم تا commit کامل نخ اول بلاک می‌ماند و بعد از آزادشدن، UserCoupون نخ اول را
از قبل می‌بیند - پیش از رسیدن به debit_points. قفل دوباره‌ی همان ردیف توسط خودِ debit_points
(چند خط پایین‌تر) در همان تراکنش/اتصال کاملاً بی‌خطر است (Re-entrant).
"""

from django.db import transaction as db_transaction
from django.db.models import Q
from django.utils import timezone

from promotions.models import Coupon, CouponRedemption, UserCoupon

from . import services as loyalty_services
from .exceptions import (
    IdempotencyKeyConflictError, RewardAlreadyRedeemedError, RewardInactiveError, RewardOutOfStockError,
)
from .models import LoyaltyAccount, LoyaltyTransaction

REWARD_SOURCE_TYPE = 'loyalty_reward'


def _validate_reward_is_redeemable(reward):
    if not reward.is_active:
        raise RewardInactiveError(f'پاداش «{reward.title}» غیرفعال است.')
    if reward.coupon.status() != Coupon.STATUS_ACTIVE:
        raise RewardInactiveError(f'کد تخفیفِ پشتِ پاداش «{reward.title}» در حال حاضر فعال/معتبر نیست.')


def _coupon_capacity_exhausted(locked_coupon):
    """ عیناً همان دو شرطی که promotions/wallet.py::claim پیش از تخصیص می‌سنجد - claim_limit
    (سقف تعداد دریافت‌کنندگان) و total_limit (سقف مصرف واقعیِ CouponRedemption)؛ per_user_limit
    عمداً اینجا سنجیده نمی‌شود - آن فیلد فقط لحظه‌ی اعمال کد سرِ خرید معنا دارد، نه لحظه‌ی تخصیص. """
    if locked_coupon.claim_limit is not None:
        if UserCoupon.objects.filter(coupon=locked_coupon).count() >= locked_coupon.claim_limit:
            return True
    if locked_coupon.total_limit is not None:
        live = Q(status=CouponRedemption.STATUS_REDEEMED) | Q(status=CouponRedemption.STATUS_RESERVED, expires_at__gt=timezone.now())
        if CouponRedemption.objects.filter(coupon=locked_coupon).filter(live).count() >= locked_coupon.total_limit:
            return True
    return False


def redeem_points_for_reward(user, reward, *, idempotency_key):
    """
    بازخرید اتمیک یک LoyaltyReward: کسر points_cost از لجر امتیاز کاربر و تخصیص کوپنِ پشتِ آن
    (promotions.UserCoupon، source=SOURCE_AUTO). طبق الگوی Master Coupon، هر کاربر حداکثر
    یک‌بار در عمر می‌تواند یک reward مشخص را بازخرید کند.

    idempotency_key: فراخوانی مکرر با همان کلید (همان کاربر، همان reward) همیشه دقیقاً همان
    جفت (LoyaltyTransaction, UserCoupon) را برمی‌گرداند. همان کلید برای reward دیگر (حتی
    هم‌قیمت) => IdempotencyKeyConflictError.

    خروجی: (LoyaltyTransaction, UserCoupon)
    """
    _validate_reward_is_redeemable(reward)

    with db_transaction.atomic():
        # گام ۱: قفل ردیفی LoyaltyAccount - خودِ ارکستریتور، پیش از هر بررسی/فراخوانی دیگری
        # (نگاه کنید توضیح رفع Double-Debit Race بالای فایل).
        account = LoyaltyAccount.get_or_create_for_user(user)
        LoyaltyAccount.objects.select_for_update().get(pk=account.pk)

        # گام ۲: بررسی «قبلاً بازخرید شده» داخل همان قفل، پیش از هر کسر امتیازی. عمداً صرفِ وجود
        # UserCoupon کافی برای رد کردن نیست - وگرنه یک Replay واقعی (همان idempotency_key، فراخوانی
        # دوم) هم اشتباهاً همین‌جا رد می‌شد. فقط وقتی رد می‌شود که UserCoupon موجود باشد *و* هیچ
        # LoyaltyTransaction ای با همین کلید/همین reward از قبل ثبت نشده باشد (یعنی این تلاش قطعاً
        # کلید تازه‌ای است، نه بازپخش تلاش قبلی‌ای که خودش این UserCoupon را ساخته).
        already_redeemed = UserCoupon.objects.filter(coupon_id=reward.coupon_id, user=user).exists()
        is_own_prior_success = LoyaltyTransaction.objects.filter(
            idempotency_key=idempotency_key, source_type=REWARD_SOURCE_TYPE, source_id=reward.pk,
        ).exists()
        if already_redeemed and not is_own_prior_success:
            raise RewardAlreadyRedeemedError(f'شما قبلاً پاداش «{reward.title}» را بازخرید کرده‌اید.')

        # گام ۳: کسر امتیاز.
        loyalty_txn = loyalty_services.debit_points(
            user, reward.points_cost, LoyaltyTransaction.REDEEM_REWARD,
            f'بازخرید پاداش «{reward.title}»', source_type=REWARD_SOURCE_TYPE, source_id=reward.pk,
            idempotency_key=idempotency_key,
        )

        # گام ۴: محافظت مالکیت (هم‌الگوی فاز ۴A).
        if loyalty_txn.account.user_id != user.id:
            raise IdempotencyKeyConflictError(f'کلید ضدتکرار «{idempotency_key}» متعلق به کاربر دیگری است.')

        # گام ۵: محافظت منبع (رفع Double-Reward، مصوبه‌ی بازبینی طراحی) - amount/transaction_type
        # به‌تنهایی دو پاداش هم‌قیمت را از هم تفکیک نمی‌کنند؛ تطابق قطعی با source الزامی است.
        if loyalty_txn.source_type != REWARD_SOURCE_TYPE or loyalty_txn.source_id != reward.pk:
            raise IdempotencyKeyConflictError(
                f'کلید ضدتکرار «{idempotency_key}» قبلاً برای پاداش دیگری استفاده شده است.'
            )

        # گام ۶: تشخیص Replay - اگر UserCoupon همین reward از قبل موجود بود، گام ۲ همین الان جلویش
        # را گرفته بود؛ رسیدن به اینجا با یک UserCoupon موجود فقط یعنی این دقیقاً همان کلید Replay
        # شده‌ی یک بازخرید قبلاً کامل‌شده است.
        existing_user_coupon = UserCoupon.objects.filter(coupon_id=reward.coupon_id, user=user).first()
        if existing_user_coupon is not None:
            return loyalty_txn, existing_user_coupon

        # گام ۷: قفل Coupon + بررسی ظرفیت (claim_limit/total_limit) - عبور از سقف => رول‌بک کامل کسر گام ۳.
        locked_coupon = Coupon.objects.select_for_update().get(pk=reward.coupon_id)
        if _coupon_capacity_exhausted(locked_coupon):
            raise RewardOutOfStockError(f'ظرفیت پاداش «{reward.title}» تمام شده است.')

        # گام ۸: تخصیص کوپن.
        user_coupon = UserCoupon.objects.create(coupon=locked_coupon, user=user, source=UserCoupon.SOURCE_AUTO)

    return loyalty_txn, user_coupon
