"""
مهلت ثبت اطلاعات چک (فاز D).

سفارش چکی در لحظه‌ی ثبت، مهلتی (Order.cheque_deadline_at = now + SiteSettings.cheque_submission_deadline_hours) می‌گیرد تا مشتری
دست‌کم یک چک ثبت کند. اگر تا پایان مهلت هیچ چکی (با هر وضعیتی) برای سفارش ثبت نشده باشد، سفارش لغو می‌شود و
رزرو موجودی آزاد می‌گردد (همان سیگنال order_canceled: آزادسازی رزرو، بازگشت سهم کیف‌پول، آزادسازی کد تخفیف).

قواعد:
  - مهلت با ثبت *اولین* چک متوقف می‌شود؛ حتی اگر بعداً رد یا حذف شود، سفارش دیگر به‌خاطر مهلت لغو نمی‌شود (مهلت فقط برای
    «هیچ چکی ثبت نشده» است؛ بررسی/اصلاح چک مسیر خودش را دارد).
  - ساعتِ ۰ در تنظیمات = لغو خودکار خاموش (کلید قطع): نه مهلتی ثبت می‌شود، نه سفارش‌های دارای مهلت لغو می‌شوند.
  - سفارش بدون cheque_deadline_at (قدیمی/ثبت‌شده توسط مدیر) هرگز خودکار لغو نمی‌شود.
  - لغو و ثبت چک هر دو روی ردیف سفارش قفل می‌گیرند (create_cheque هم همین کار را می‌کند)؛ پس مسابقه‌ی «ثبت چک هم‌زمان با لغو»
    یکی را برنده می‌کند و دیگری وضعیت تازه را می‌بیند (ثبت چکِ سفارش لغوشده ← ۴۰۹).
"""
import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from products.models import SiteSettings

from .models import Order

logger = logging.getLogger(__name__)

CANCEL_REASON = 'عدم ثبت اطلاعات چک در مهلت مقرر'
BATCH_LIMIT = 200


def deadline_hours():
    """ مهلت جاری به ساعت؛ ۰ = خاموش """
    return int(SiteSettings.cached().cheque_submission_deadline_hours or 0)


def initial_deadline(now=None):
    """ مهلتِ سفارش چکیِ تازه (None وقتی مهلت خاموش است) """
    hours = deadline_hours()
    if hours <= 0:
        return None
    return (now or timezone.now()) + timedelta(hours=hours)


def _has_cheque(order):
    return order.cheques.exists()                       # هر وضعیتی، حتی حذف‌شده: مهلت با ثبت اولین چک متوقف شده است


def waiting_for_cheque(order):
    """ آیا مهلتِ این سفارش هنوز «فعال» است (چکی، دارای مهلت، باز، بدون چک و غیرپرداخت‌شده)؟ """
    if not order.cheque_deadline_at or not order.is_cheque:
        return False
    if order.status != 'pending' or order.approved_at or order.is_paid:
        return False
    return not _has_cheque(order)


def format_remaining(seconds):
    """ ۹۰۰۰ ثانیه ← «۲ ساعت و ۳۰ دقیقه» """
    minutes = max(1, (int(seconds) + 59) // 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = []
    if days:
        parts.append(f'{days} روز')
    if hours:
        parts.append(f'{hours} ساعت')
    if minutes and not days:
        parts.append(f'{minutes} دقیقه')
    return ' و '.join(parts) or '۱ دقیقه'


def deadline_notice(order, now=None):
    """
    اطلاعات نمایش مهلت به مشتری یا None (وقتی مهلتی در کار نیست: چک ثبت شده، سفارش بسته، یا مهلت خاموش).
    {'iso': ISO، 'expired': bool, 'seconds': باقی‌مانده، 'text': متن فارسی}
    """
    if deadline_hours() <= 0 or not waiting_for_cheque(order):
        return None
    now = now or timezone.now()
    seconds = int((order.cheque_deadline_at - now).total_seconds())
    iso = order.cheque_deadline_at.isoformat()
    if seconds <= 0:
        return {'iso': iso, 'expired': True, 'seconds': 0,
                'text': 'مهلت ثبت چک به پایان رسیده است؛ سفارش به‌زودی به‌صورت خودکار لغو می‌شود.'}
    return {'iso': iso, 'expired': False, 'seconds': seconds,
            'text': f'برای حفظ سفارش، اطلاعات چک را تا {format_remaining(seconds)} دیگر ثبت کنید؛ در غیر این صورت سفارش لغو می‌شود.'}


def cancel_if_expired(order_id, now=None):
    """
    لغوِ اتمیک یک سفارش اگر همچنان منتظر چک است و مهلتش گذشته. قفل ردیف سفارش، شرط‌ها را دوباره می‌سنجد.
    True فقط وقتی همین فراخوانی لغو کرد. آزادسازی رزرو با سیگنال order_canceled (بعد از commit) انجام می‌شود.
    """
    now = now or timezone.now()
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        if not order.cheque_deadline_at or order.cheque_deadline_at > now:
            return False
        if not waiting_for_cheque(order):
            return False
        order.status = 'canceled'
        order.cancel_reason = CANCEL_REASON
        order.save(update_fields=['status', 'cancel_reason', 'canceled_at', 'updated_at'])
    return True


def cancel_expired_cheque_orders(now=None, limit=BATCH_LIMIT):
    """
    لغو سفارش‌های چکیِ منقضی‌شده‌ی بی‌چک (تسک دوره‌ی Celery Beat). با تنظیم ۰ ساعت هیچ کاری نمی‌کند. خطای یک سفارش بقیه را
    متوقف نمی‌کند. ← تعداد سفارش‌های لغوشده
    """
    if deadline_hours() <= 0:
        return 0
    now = now or timezone.now()
    candidates = list(Order.objects.filter(status='pending', approved_at__isnull=True, cheque_deadline_at__isnull=False,
                                           cheque_deadline_at__lte=now)
                      .order_by('cheque_deadline_at').values_list('pk', flat=True)[:limit])
    canceled = 0
    for order_id in candidates:
        try:
            if cancel_if_expired(order_id, now):
                canceled += 1
                logger.info('سفارش چکی %s به‌دلیل پایان مهلت ثبت چک لغو شد.', order_id)
        except Exception:  # noqa: BLE001 - خطای یک سفارش نباید جاروی بقیه را بشکند
            logger.exception('لغو خودکار سفارش چکی %s ناموفق بود.', order_id)
    return canceled
