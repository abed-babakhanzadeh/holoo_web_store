"""
ابزار تست چرخه‌ی تأیید تجاری (برای تست‌های همه‌ی اپ‌ها).

با فاز ۲ (products.pricing.is_price_hidden) و گیت can_order() روی سبد/تسویه‌حساب، یک
CustomUser.objects.create_user() ساده دیگر به‌تنهایی «مشتری عادی و مجاز» نیست — approval_status
پیش‌فرض PENDING است، پس قیمت برایش پنهان و اکشن‌های سبد/checkout برایش مسدود می‌شوند.
اکثر تست‌های سبد/سفارش/تخفیفِ پروژه دقیقاً همان «مشتری عادی» را فرض می‌گیرند؛ به‌جای پراکنده
approve() زدن در هر فایل، همین‌جا یک‌بار ساخته می‌شود.
"""

from itertools import count

from .models import CustomUser

_national_code_seq = count(1000000001)


def make_approved_user(phone_number, price_level=1, **extra_fields):
    """
    کاربرِ عادیِ تأییدشده برای تست: پروفایل کامل (تا CheckConstraint رد نکند) + approval_status=
    APPROVED با همان price_level. امضا عمداً هم‌شکل CustomUser.objects.create_user است.
    """
    extra_fields.setdefault('first_name', 'کاربر')
    extra_fields.setdefault('last_name', 'تست')
    extra_fields.setdefault('national_code', str(next(_national_code_seq)))
    user = CustomUser.objects.create_user(phone_number=phone_number, price_level=price_level, **extra_fields)
    user, _ = user.approve(price_level=price_level)
    return user
