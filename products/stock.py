"""
موجودی قابل‌فروش و رزرو اتمیک موجودی در دیتابیس سایت.

دو ستون با دو مالک جدا (قرارداد کل پروژه؛ نگاه کنید holoo/product_state.py):
  Product.stock             ← فقط توسط همگام‌سازی هلو نوشته می‌شود
  Product.reserved_quantity ← فقط توسط همین ماژول (رزرو سفارش‌های سایت) نوشته می‌شود
  موجودی قابل‌فروش = floor(stock) − reserved_quantity − بافر اطمینان (SiteSettings.stock_safety_buffer)
  و هرگز ذخیره نمی‌شود.

رزرو یک دستور UPDATE شرطی و تک‌دستوری است (نه «بخوان، بررسی کن، بنویس»):

    UPDATE products SET reserved_quantity = reserved_quantity + q
    WHERE id = X AND stock >= reserved_quantity + q + بافر

دو خریدار هم‌زمان آخرین قلم: دستور روی ردیف قفل می‌گیرد و فقط یکی rowcount=1 می‌گیرد. قفل Redis لازم نیست (و با commit
دیتابیس اتمیک هم نبود). سبد چندقلمی به ترتیب شناسه‌ی محصول رزرو می‌کند (جلوگیری از deadlock) و شکست هر قلم باعث می‌شود
فراخوان‌کننده کل تراکنش را برگرداند (InsufficientStock باید از داخل transaction.atomic بالا برود).

دفتر رزرو (StockReservation) چرخه‌ی عمر را نگه می‌دارد، و ستون reserved_quantity را می‌شود هر لحظه از روی آن بازمحاسبه
کرد (audit_reserved_counters):
  held      ← سفارش ثبت شده؛ شمرده می‌شود. اگر expires_at دارد (پرداخت آنلاین هنوز تمام‌نشده) پس از آن منقضی می‌شود؛
              وگرنه (پرداخت‌شده/چکی در انتظار تأیید مدیر) تا تصمیم مدیر می‌ماند.
  invoiced  ← فاکتور در هلو ثبت شد؛ هنوز شمرده می‌شود چون Few هلو شاید هنوز کم نشده یا سینک قدیمی است. فقط وقتی آزاد
              می‌شود که سینکی که *بعد از* ثبت فاکتور شروع شده، موجودی همان کالا را به‌روز کرده باشد (release_synced).
  expired / released ← شمرده نمی‌شود.
خطا همیشه به سمت «کم‌فروشیِ موقت» است، نه فروش بدون موجودی.

این ماژول هیچ اپ بالادستی (orders/payments/holoo) را import نمی‌کند؛ سفارش را فقط با order_id (عدد) می‌شناسد، مثل کد تخفیف.
"""

import logging
import math
from datetime import timedelta

from django.db import transaction
from django.db.models import Case, ExpressionWrapper, F, FloatField, IntegerField, Q, Sum, Value, When
from django.utils import timezone

from .models import Product, SiteSettings, StockReservation

logger = logging.getLogger(__name__)

# مهلت رزرو موقت تا نهایی‌شدن پرداخت (تصمیم مدیریت: ۲۰ دقیقه). با هر شروع پرداخت از نو شروع می‌شود.
RESERVATION_TTL = timedelta(minutes=20)

ACTIVE_STATES = (StockReservation.HELD, StockReservation.INVOICED)


class InsufficientStock(Exception):
    """ shortages: فهرست {'product': Product, 'requested': int, 'available': int} """
    def __init__(self, shortages):
        self.shortages = shortages
        super().__init__('; '.join(
            f"{s['product'].name}: درخواست {s['requested']}، قابل‌فروش {s['available']}" for s in shortages
        ))


def safety_buffer():
    """ بافر اطمینان (۰/۱/۲) از تنظیمات سایت؛ هاردکد نیست """
    try:
        return max(0, int(SiteSettings.cached().stock_safety_buffer or 0))
    except Exception:                                    # تنظیمات در دسترس نبود؛ پیش‌فرض بدون بافر
        return 0


def compute_available(stock, reserved, buffer=0):
    """ موجودی قابل‌فروش (عدد صحیح، هرگز منفی): floor(stock) − رزروشده − بافر """
    try:
        whole = math.floor(float(stock) + 1e-9)
    except (TypeError, ValueError):
        whole = 0
    return max(0, whole - int(reserved or 0) - int(buffer or 0))


def available_of(product, buffer=None):
    return compute_available(product.stock, product.reserved_quantity, safety_buffer() if buffer is None else buffer)


def available_expression(buffer=None):
    """ همان فرمول به‌صورت عبارت دیتابیس (برای فیلتر «فقط موجودها» و ترتیب موجود/ناموجود) """
    buffer = safety_buffer() if buffer is None else buffer
    return ExpressionWrapper(F('stock') - F('reserved_quantity') - Value(int(buffer)), output_field=FloatField())


# ------------------------------------------------------------------ ابزار سطح پایین

def _claim(product_id, quantity, buffer):
    return Product.objects.filter(
        pk=product_id, stock__gte=F('reserved_quantity') + int(quantity) + int(buffer),
    ).update(reserved_quantity=F('reserved_quantity') + int(quantity)) == 1


def _unclaim(product_id, quantity):
    """ کاهش شمارنده؛ هرگز زیر صفر نمی‌رود (ستون مثبت است) """
    Product.objects.filter(pk=product_id).update(reserved_quantity=Case(
        When(reserved_quantity__gte=int(quantity), then=F('reserved_quantity') - int(quantity)),
        default=Value(0), output_field=IntegerField(),
    ))


def _end_row(row_id, from_states, to_state, reason, now):
    """ پایان یک ردیف دفتر + آزادسازی شمارنده، فقط یک‌بار (اگر کس دیگری زودتر تمامش کرد 0 برمی‌گردد) """
    with transaction.atomic():
        row = StockReservation.objects.select_for_update().filter(pk=row_id, state__in=from_states).first()
        if row is None:
            return 0
        row.state, row.released_at, row.release_reason = to_state, now, reason
        row.save(update_fields=['state', 'released_at', 'release_reason', 'updated_at'])
        _unclaim(row.product_id, row.quantity)
        return 1


def _shortage(product_id, quantity, buffer):
    product = Product.objects.get(pk=product_id)
    return {'product': product, 'requested': int(quantity), 'available': available_of(product, buffer)}


# ------------------------------------------------------------------ رزرو

def reserve_for_order(order_id, lines, *, expires_at=None, now=None, buffer=None):
    """
    رزرو اتمیک برای یک سفارش؛ باید داخل transaction.atomic صدا زده شود و InsufficientStock را بدون بلعیدن بالا بدهد.
    lines: {product_id: تعداد}. idempotent است: ردیف فعالِ موجود فقط مهلتش تازه می‌شود؛ ردیف منقضی/آزادشده دوباره
    (و دوباره‌ی اتمیک) رزرو می‌شود.
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('reserve_for_order باید داخل transaction.atomic صدا زده شود.')
    now = now or timezone.now()
    buffer = safety_buffer() if buffer is None else buffer
    ids = sorted(lines)
    rows = {r.product_id: r for r in StockReservation.objects.select_for_update().filter(order_id=order_id, product_id__in=ids)}
    shortages = []
    for product_id in ids:
        quantity = int(lines[product_id])
        row = rows.get(product_id)
        if row is not None and row.state in ACTIVE_STATES:
            if row.state == StockReservation.HELD:
                row.expires_at = expires_at
                row.save(update_fields=['expires_at', 'updated_at'])
            continue
        claimed = _claim(product_id, quantity, buffer)
        if not claimed:
            expire_stale(now=now, product_ids=[product_id])        # شاید رزرو منقضیِ دیگری راهش را بسته باشد
            claimed = _claim(product_id, quantity, buffer)
        if not claimed:
            shortages.append(_shortage(product_id, quantity, buffer))
            continue
        if row is None:
            StockReservation.objects.create(order_id=order_id, product_id=product_id, quantity=quantity,
                                            state=StockReservation.HELD, expires_at=expires_at)
        else:
            row.quantity, row.state, row.expires_at = quantity, StockReservation.HELD, expires_at
            row.invoiced_at = row.released_at = None
            row.release_reason = ''
            row.save()
    if shortages:
        raise InsufficientStock(shortages)


def confirm_hold(order_id):
    """ پرداخت نهایی شد (یا سفارش چکی/در انتظار تأیید است): مهلت برداشته می‌شود و رزرو تا تصمیم مدیر می‌ماند """
    return StockReservation.objects.filter(order_id=order_id, state=StockReservation.HELD).update(expires_at=None)


def extend_hold(order_id, now=None):
    """ شروع تلاش پرداخت: مهلت ۲۰ دقیقه از همین لحظه (فقط ردیف‌های هنوز در مهلت) """
    now = now or timezone.now()
    return StockReservation.objects.filter(
        order_id=order_id, state=StockReservation.HELD, expires_at__isnull=False,
    ).update(expires_at=now + RESERVATION_TTL)


def mark_invoiced(order_id, now=None):
    """ فاکتور در هلو ثبت شد؛ رزرو تا سینک بعدیِ موجودی نگه داشته می‌شود (release_synced) """
    now = now or timezone.now()
    return StockReservation.objects.filter(order_id=order_id, state__in=ACTIVE_STATES).update(
        state=StockReservation.INVOICED, invoiced_at=now, expires_at=None,
    )


# ------------------------------------------------------------------ آزادسازی

def release_order(order_id, reason, now=None):
    """ آزادسازی همه‌ی رزروهای فعال یک سفارش (لغو/رد/انصراف) """
    now = now or timezone.now()
    ids = list(StockReservation.objects.filter(order_id=order_id, state__in=ACTIVE_STATES).values_list('pk', flat=True))
    return sum(_end_row(pk, ACTIVE_STATES, StockReservation.RELEASED, reason, now) for pk in ids)


def expire_stale(now=None, product_ids=None):
    """ رزروهای در انتظارِ پرداخت که مهلتشان گذشته منقضی می‌شوند (فقط حالت held و دارای expires_at) """
    now = now or timezone.now()
    qs = StockReservation.objects.filter(state=StockReservation.HELD, expires_at__isnull=False, expires_at__lt=now)
    if product_ids is not None:
        qs = qs.filter(product_id__in=list(product_ids))
    return sum(_end_row(pk, (StockReservation.HELD,), StockReservation.EXPIRED, 'expired', now)
               for pk in list(qs.values_list('pk', flat=True)))


def release_synced(now=None):
    """
    رزروهای invoiced که سینکِ بعد از ثبت فاکتور موجودی کالایشان را به‌روز کرده آزاد می‌شوند (Few هلو دیگر خودش
    کسر را دارد؛ نگه‌داشتن رزرو یعنی شمارش مضاعف). اگر سینک سالم نباشد چیزی آزاد نمی‌شود: محافظه‌کارانه.
    """
    now = now or timezone.now()
    ids = list(StockReservation.objects.filter(
        state=StockReservation.INVOICED, invoiced_at__isnull=False, product__stock_synced_at__gt=F('invoiced_at'),
    ).values_list('pk', flat=True))
    return sum(_end_row(pk, (StockReservation.INVOICED,), StockReservation.RELEASED, 'synced', now) for pk in ids)


# ------------------------------------------------------------------ ممیزی ناوردایی

def audit_reserved_counters(fix=False):
    """
    reserved_quantity هر کالا باید با جمع رزروهای فعالش در دفتر برابر باشد. فهرست مغایرت‌ها (و در صورت fix=True
    اصلاحشان با دفتر به‌عنوان مرجع) را برمی‌گرداند: [{'product_id', 'counter', 'ledger'}].
    """
    ledger = dict(
        StockReservation.objects.filter(state__in=ACTIVE_STATES).values('product_id')
        .annotate(total=Sum('quantity')).values_list('product_id', 'total')
    )
    mismatches = []
    for product_id, counter in Product.objects.filter(Q(reserved_quantity__gt=0) | Q(pk__in=list(ledger))).values_list('pk', 'reserved_quantity'):
        expected = int(ledger.get(product_id, 0))
        if counter != expected:
            mismatches.append({'product_id': product_id, 'counter': counter, 'ledger': expected})
            if fix:
                Product.objects.filter(pk=product_id).update(reserved_quantity=expected)
    if mismatches:
        logger.error("ممیزی رزرو موجودی: %s مغایرت (fix=%s): %s", len(mismatches), fix, mismatches[:10])
    return mismatches
