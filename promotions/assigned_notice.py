"""
اطلاع‌رسانی پیامکی «کد تخفیف اختصاصی برای شما صادر شد».

حسابرسی ۱۸ اکتبر ۲۰۲۶: وقتی مدیر در ادمین یک کد را به کاربری تخصیص می‌داد (UserCoupon)، کاربر هیچ‌جا مطلع نمی‌شد و فقط اگر
خودش سر می‌زد، کد را در «کدهای من» پنل می‌دید. حالا پیامک می‌رود: عنوان، مقدار، شمول و اعتبار، خودِ کد و اشاره به «کدهای من».

محدوده: فقط تخصیصی که *مدیر از ادمین* ثبت می‌کند (CouponAdmin.save_formset). کدی که کاربر خودش دریافت می‌کند («دریافت کد
جدید») یا پاداش خودکار باشگاه است، پیامک اضافه نمی‌گیرد (کاربر همان لحظه نتیجه را می‌بیند). پیامک عمداً لینک ندارد: خط
پیامک ملی‌پیامک متن حاوی لینک را رد می‌کند (کد ۱۴) و پیام هرگز نمی‌رسد. کدِ اختصاصی فقط برای همان کاربر کار می‌کند (coupons.py)، پس درج آن در
پیامکِ خودش نشتی نیست. روشن/خاموش بودن و متن پیام از «تنظیمات اطلاع‌رسانی» ادمین است (قالب coupon_assigned_customer).
"""

import logging

from .models import Coupon
from .wallet import jalali_date

logger = logging.getLogger(__name__)

MAX_SCOPE_NAMES = 2


def _scope_text(coupon):
    if coupon.kind == Coupon.KIND_FREE_SHIPPING:
        return 'ارسال رایگان با پیک درون‌شهری'
    if coupon.scope == Coupon.SCOPE_CART:
        return 'روی کل سبد'
    names = [p.name for p in coupon.products.all()] + [c.name for c in coupon.categories.all()]
    if not names:
        return ''
    text = 'فقط برای ' + '، '.join(names[:MAX_SCOPE_NAMES])
    return text + (f' و {len(names) - MAX_SCOPE_NAMES} مورد دیگر' if len(names) > MAX_SCOPE_NAMES else '')


def _details(coupon):
    parts = [coupon.short_display if coupon.kind != Coupon.KIND_FREE_SHIPPING else '', _scope_text(coupon)]
    if coupon.min_cart_amount:
        parts.append(f'حداقل سبد {coupon.min_cart_amount:,} تومان')
    if coupon.ends_at:
        parts.append(f'اعتبار تا {jalali_date(coupon.ends_at)}')
    return '، '.join(part for part in parts if part)


def notify_assigned(request, coupon, user):
    """ پیامک تخصیص کد به یک کاربر؛ Notification یا None (کد غیرفعال/منقضی، یا ارسال خاموش است). هرگز استثنا نمی‌دهد """
    from notifications.service import notify

    if coupon.status() not in (Coupon.STATUS_ACTIVE, Coupon.STATUS_SCHEDULED):
        return None
    try:
        name = ' '.join(part for part in ((user.first_name or '').strip(), (user.last_name or '').strip()) if part)
        return notify(
            user.phone_number, 'coupon_assigned_customer', name=name or 'کاربر', title=coupon.title,
            details=_details(coupon), code=coupon.code,
        )
    except Exception:  # noqa: BLE001 - شکستِ پیامک نباید ذخیره‌ی ادمین را بشکند
        logger.exception('پیامک تخصیص کد %s به کاربر %s ناموفق بود.', coupon.code, user.pk)
        return None
