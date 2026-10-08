"""
تسک‌های دوره‌ای سفارش. زمان‌بندی در config/celery.py ثبت شده است (نه settings.py).
"""
import logging

from celery import shared_task

from . import deadline

logger = logging.getLogger(__name__)


@shared_task
def cancel_expired_cheque_orders():
    """
    سفارش‌های چکیِ بدون چکِ ثبت‌شده که مهلتشان گذشته لغو و رزرو موجودی‌شان آزاد می‌شود (orders/deadline.py). هر ۱۵ دقیقه.
    """
    canceled = deadline.cancel_expired_cheque_orders()
    if canceled:
        logger.info('لغو خودکار سفارش چکی: %s سفارش لغو شد.', canceled)
    return f'canceled={canceled}'
