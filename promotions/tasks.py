from celery import shared_task

from . import coupons


@shared_task
def release_expired_coupon_reservations():
    """
    رزروهای پرداخت‌نشده‌ی از مهلت گذشته را «آزادشده» علامت می‌زند. ظرفیت کد این تسک را لازم ندارد (رزرو منقضی خودش
    نمی‌شمارد)؛ کارش فقط پاک‌ماندن وضعیت‌ها و گزارش درست ادمین است. idempotent.
    """
    return coupons.release_expired()
