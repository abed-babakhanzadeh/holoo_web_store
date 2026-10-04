"""
تسک‌های دوره‌ای رزرو موجودی (products/stock.py). درستی رزرو به این تسک‌ها وابسته نیست (رزرو منقضی هنگام کمبود موجودی
خودش پاک می‌شود)؛ این‌ها فقط وضعیت‌ها و شمارنده را مرتب نگه می‌دارند.
"""
from celery import shared_task

from . import stock


@shared_task
def expire_stock_reservations():
    """ رزروهای پرداخت‌نشده‌ی از مهلت گذشته (۲۰ دقیقه) منقضی و رزروهای فاکتورشده‌ی هم‌گام‌شده آزاد می‌شوند """
    expired = stock.expire_stale()
    released = stock.release_synced()
    return f'expired={expired} released_synced={released}'


@shared_task
def audit_stock_reservations():
    """ ممیزی شبانه: reserved_quantity هر کالا باید با جمع رزروهای فعال دفتر برابر باشد؛ مغایرت با دفتر اصلاح و لاگ می‌شود """
    return f'mismatches={len(stock.audit_reserved_counters(fix=True))}'
