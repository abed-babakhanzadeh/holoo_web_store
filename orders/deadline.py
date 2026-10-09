"""
مهلت ثبت/اصلاح اطلاعات چک (فاز D، با اصلاح فاز E).

سفارش چکی در لحظه‌ی ثبت، مهلتی (Order.cheque_deadline_at = now + SiteSettings.cheque_submission_deadline_hours) می‌گیرد تا مشتری
دست‌کم یک چکِ *فعال* (pending_review یا approved) داشته باشد. اگر تا پایان مهلت هیچ چکِ فعالی نباشد، سفارش لغو می‌شود و
رزرو موجودی آزاد می‌گردد (همان سیگنال order_canceled: آزادسازی رزرو، بازگشت سهم کیف‌پول، آزادسازی کد تخفیف).

ساعتِ مهلت = «ستونِ cheque_deadline_at خالی نیست»:
  - ثبت چک یا ارسال مجدد چکِ اصلاح‌شده (چک فعال شد) ← ساعت متوقف می‌شود (ستون خالی می‌شود).
  - ردِ چک توسط مدیر طوری که دیگر هیچ چکِ فعالی نماند ← یک پنجره‌ی تازه به همان طول تنظیم‌شده (از لحظه‌ی رد) برای اصلاح یا
    ثبت چک جایگزین شروع می‌شود. اگر مشتری تا پایانش چک فعال نداشته باشد، سفارش لغو و رزرو آزاد می‌شود. هر ردِ دیگر پنجره را
    دوباره تازه می‌کند (هر بار تصمیمِ یک انسان است).
  - حذفِ چکِ ردشده توسط مشتری مهلت را تمدید نمی‌کند (پنجره‌ی ردِ قبلی ادامه دارد).
  - تأییدِ مدیر ساعت را متوقف می‌کند؛ بازگشت به pending هم همین‌طور.

قواعد دیگر:
  - ساعتِ ۰ در تنظیمات = لغو خودکار خاموش (کلید قطع): نه مهلتی ثبت می‌شود، نه سفارش‌های دارای مهلت لغو می‌شوند.
  - سفارش بدون cheque_deadline_at (قدیمی/ثبت‌شده توسط مدیر) هرگز خودکار لغو نمی‌شود، تا وقتی ردِ چک پنجره‌ای برایش باز کند.
  - لغو و ثبت/بررسی چک هر دو روی ردیف سفارش قفل می‌گیرند؛ پس مسابقه‌ها یکی را برنده می‌کنند و دیگری وضعیت تازه را می‌بیند
    (ثبت چکِ سفارش لغوشده ← ۴۰۹).
"""
import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from products.models import SiteSettings

from .models import ChequePayment, Order
from .signals import cheque_deadline_expired

logger = logging.getLogger(__name__)

CANCEL_REASON = 'عدم ثبت اطلاعات چک در مهلت مقرر'
CANCEL_REASON_CORRECTION = 'عدم اصلاح یا ثبت چک جایگزین در مهلت مقرر'
BATCH_LIMIT = 200
ACTIVE_STATUSES = (ChequePayment.STATUS_PENDING, ChequePayment.STATUS_APPROVED, ChequePayment.STATUS_CLEARED)


def deadline_hours():
    """ مهلت جاری به ساعت؛ ۰ = خاموش """
    return int(SiteSettings.cached().cheque_submission_deadline_hours or 0)


def initial_deadline(now=None):
    """ مهلتِ سفارش چکیِ تازه / پنجره‌ی تازه‌ی اصلاح (None وقتی مهلت خاموش است) """
    hours = deadline_hours()
    if hours <= 0:
        return None
    return (now or timezone.now()) + timedelta(hours=hours)


def has_active_cheque(order):
    """ آیا سفارش دست‌کم یک چک در حال بررسی یا تأییدشده دارد؟ (ردشده/حذف‌شده «فعال» نیست) """
    return order.cheques.filter(status__in=ACTIVE_STATUSES).exists()


def stop_clock(order):
    """ چک فعال شد: مهلت متوقف می‌شود (داخل تراکنشِ قفل‌شده‌ی فراخوان‌کننده صدا بزنید) """
    Order.objects.filter(pk=order.pk).update(cheque_deadline_at=None)
    order.cheque_deadline_at = None


def restart_clock(order, now=None):
    """ هیچ چک فعالی نمانده (رد شد): پنجره‌ی تازه از همین لحظه. مهلت خاموش ← بدون مهلت. ← مهلت جدید یا None """
    new_deadline = initial_deadline(now)
    Order.objects.filter(pk=order.pk).update(cheque_deadline_at=new_deadline)
    order.cheque_deadline_at = new_deadline
    return new_deadline


def waiting_for_cheque(order):
    """ آیا ساعتِ این سفارش روشن است (چکی، دارای مهلت، باز، غیرپرداخت‌شده و بدون هیچ چک فعال)؟ """
    if not order.cheque_deadline_at or not order.is_cheque:
        return False
    if order.status != 'pending' or order.approved_at or order.is_paid:
        return False
    return not has_active_cheque(order)


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


def deadline_note(order, now=None):
    """ عبارت کوتاه مهلت برای پیامک: «ظرف ۲۴ ساعت» یا «در اسرع وقت» (وقتی مهلتی در کار نیست) """
    if not order.cheque_deadline_at:
        return 'در اسرع وقت'
    seconds = int((order.cheque_deadline_at - (now or timezone.now())).total_seconds())
    if seconds <= 0:
        return 'در اسرع وقت'
    if 3600 <= seconds < 48 * 3600:
        return f'ظرف {round(seconds / 3600)} ساعت'                 # مهلت تنظیمی معمولاً ساعتی است: «ظرف ۲۴ ساعت» نه «۱ روز»
    return f'ظرف {format_remaining(seconds)}'


def deadline_notice(order, now=None):
    """
    اطلاعات نمایش مهلت به مشتری یا None (وقتی ساعتی روشن نیست: چک فعال دارد، سفارش بسته است، یا مهلت خاموش).
    {'iso', 'expired', 'seconds', 'kind': 'register'|'correct', 'text'}
    kind=correct یعنی چک‌های قبلی ردشده/حذف‌شده‌اند و مشتری باید اصلاح یا جایگزین ثبت کند.
    """
    if deadline_hours() <= 0 or not waiting_for_cheque(order):
        return None
    now = now or timezone.now()
    seconds = int((order.cheque_deadline_at - now).total_seconds())
    iso = order.cheque_deadline_at.isoformat()
    correcting = order.cheques.exists()
    kind = 'correct' if correcting else 'register'
    if seconds <= 0:
        return {'iso': iso, 'expired': True, 'seconds': 0, 'kind': kind,
                'text': 'مهلت ثبت چک به پایان رسیده است؛ سفارش به‌زودی به‌صورت خودکار لغو می‌شود.'}
    action = 'چک ردشده را اصلاح یا چک جایگزین را ثبت کنید' if correcting else 'اطلاعات چک را ثبت کنید'
    return {'iso': iso, 'expired': False, 'seconds': seconds, 'kind': kind,
            'text': f'برای حفظ سفارش، تا {format_remaining(seconds)} دیگر {action}؛ در غیر این صورت سفارش لغو می‌شود.'}


def cancel_if_expired(order_id, now=None):
    """
    لغوِ اتمیک یک سفارش اگر همچنان بدون چکِ فعال است و مهلتش گذشته. قفل ردیف سفارش، شرط‌ها را دوباره می‌سنجد.
    True فقط وقتی همین فراخوانی لغو کرد. آزادسازی رزرو با سیگنال order_canceled (بعد از commit) انجام می‌شود و اعلان مشتری
    با cheque_deadline_expired.
    """
    now = now or timezone.now()
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        if not order.cheque_deadline_at or order.cheque_deadline_at > now:
            return False
        if not waiting_for_cheque(order):
            return False
        order.status = 'canceled'
        order.cancel_reason = CANCEL_REASON_CORRECTION if order.cheques.exists() else CANCEL_REASON
        order.save(update_fields=['status', 'cancel_reason', 'canceled_at', 'updated_at'])
        transaction.on_commit(lambda: cheque_deadline_expired.send_robust(sender=Order, order=order))
    return True


def cancel_expired_cheque_orders(now=None, limit=BATCH_LIMIT):
    """
    لغو سفارش‌های چکیِ منقضی‌شده‌ی بدونِ چکِ فعال (تسک دوره‌ی Celery Beat). با تنظیم ۰ ساعت هیچ کاری نمی‌کند. سفارش‌های دارای چکِ
    فعال از خودِ کوئری حذف می‌شوند تا سقف دسته (limit) را اشغال نکنند. خطای یک سفارش بقیه را متوقف نمی‌کند.
    ← تعداد سفارش‌های لغوشده
    """
    if deadline_hours() <= 0:
        return 0
    now = now or timezone.now()
    active = ChequePayment.objects.filter(order=OuterRef('pk'), status__in=ACTIVE_STATUSES)
    candidates = list(Order.objects.filter(status='pending', approved_at__isnull=True, cheque_deadline_at__isnull=False,
                                           cheque_deadline_at__lte=now)
                      .filter(~Exists(active))
                      .order_by('cheque_deadline_at').values_list('pk', flat=True)[:limit])
    canceled = 0
    for order_id in candidates:
        try:
            if cancel_if_expired(order_id, now):
                canceled += 1
                logger.info('سفارش چکی %s به‌دلیل پایان مهلت ثبت/اصلاح چک لغو شد.', order_id)
        except Exception:  # noqa: BLE001 - خطای یک سفارش نباید جاروی بقیه را بشکند
            logger.exception('لغو خودکار سفارش چکی %s ناموفق بود.', order_id)
    return canceled
