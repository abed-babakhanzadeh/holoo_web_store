"""
«معین حساب» مشتری: گردش حساب خودِ مشتری در نرم‌افزار حسابداری، فقط‌خواندنی.

accounts نرم‌افزار حسابداری را نمی‌شناسد؛ مثل reviews/purchases.py تأمین‌کننده‌ی داده خودش را ثبت می‌کند (holoo/ledger.py،
از holoo/apps.py). قرارداد تأمین‌کننده: تابعی با امضای (user) که دیکشنری زیر را برمی‌گرداند:

    {'status': 'ok' | 'not_linked' | 'unavailable',
     'entries': [{'ref': '23883', 'date': datetime.date, 'bed': Decimal, 'bes': Decimal, 'comment': '...'}, ...]}

  ok          ← گردش خوانده شد (ممکن است خالی باشد)
  not_linked  ← مشتری هنوز در حسابداری ساخته نشده (طرف‌حساب ندارد)
  unavailable ← حسابداری در دسترس نیست (قطعی/خاموش)؛ صفحه پیام ملایم نشان می‌دهد و خطا نمی‌دهد

«بدهکار» = مبلغی که مشتری به فروشگاه بدهکار می‌شود (فاکتور فروش)، «بستانکار» = پرداخت/برگشت. مانده‌ی هر ردیف تجمعی است؛
مثبت = بدهکارِ مشتری، منفی = بستانکارِ مشتری. مانده از خودِ ردیف‌ها حساب می‌شود (نه از تماس جدا) تا جدول و کارت بالا همیشه یکی باشند.
"""

import csv
import io
import logging
from datetime import date, timedelta
from decimal import Decimal

import jdatetime
from django.core.cache import cache
from django.utils import timezone

from services.text import to_latin_digits

logger = logging.getLogger(__name__)

CACHE_SECONDS = 180            # گردش حساب لحظه‌ای عوض نمی‌شود؛ چند دقیقه کش، با دکمه‌ی «به‌روزرسانی»
REFRESH_COOLDOWN_SECONDS = 15  # حداقل فاصله‌ی دو تازه‌سازی اجباری برای هر کاربر (پیشگیری از فشار روی هلو)
PAGE_SIZE = 30

_provider = None


def register_provider(func):
    """ تأمین‌کننده‌ی گردش حساب (user ← دیکشنری قرارداد بالا) را ثبت می‌کند؛ آخرین ثبت معتبر است """
    global _provider
    _provider = func
    return func


def _cache_key(user):
    return f'accounts:ledger:{user.pk}:{(user.holoo_bed_sarfasl or "").strip()}'


def load_entries(user, refresh=False):
    """ (نتیجه‌ی تأمین‌کننده، تازه‌خوانده‌شده؟) — با کش کوتاه و محدودیت تازه‌سازی دستی """
    if _provider is None:
        return {'status': 'unavailable', 'entries': []}, False
    key = _cache_key(user)
    if refresh:
        allowed = True
        try:
            allowed = bool(cache.add(f'{key}:cooldown', 1, REFRESH_COOLDOWN_SECONDS))
        except Exception:  # noqa: BLE001
            pass
        if not allowed:
            refresh = False
    if not refresh:
        try:
            cached = cache.get(key)
        except Exception:  # noqa: BLE001
            cached = None
        if cached is not None:
            return cached, False
    try:
        result = _provider(user)
    except Exception:  # noqa: BLE001 - حسابداری خراب بودن نباید صفحه را بشکند
        logger.exception('خواندن معین حساب کاربر %s ناموفق بود.', user.pk)
        return {'status': 'unavailable', 'entries': []}, False
    result = dict(result, fetched_at=timezone.now())
    if result.get('status') == 'ok':
        try:
            cache.set(key, result, CACHE_SECONDS)
        except Exception:  # noqa: BLE001
            pass
    return result, True


def classify(comment):
    """ نوع ردیف برای برچسب/رنگ؛ فقط از روی متن سند هلو (ی/ي و ک/ك یکدست می‌شوند) """
    text = (comment or '').replace('ي', 'ی').replace('ك', 'ک')
    if 'افتتاحی' in text:
        return 'opening', 'افتتاحیه'
    if 'برگشت' in text:
        return 'return', 'برگشت از فروش'
    if 'فاکتور فروش' in text:
        return 'invoice', 'فاکتور فروش'
    if 'دریافت' in text or 'پرداخت' in text or 'حواله' in text or 'چک' in text:
        return 'payment', 'دریافت وجه'
    return 'other', 'سند'


def _sort_key(entry):
    ref = str(entry.get('ref') or '')
    return entry['date'], int(ref) if ref.isdigit() else 0, ref


def build_rows(entries):
    """ ردیف‌ها به ترتیب زمانی صعودی با مانده‌ی تجمعی (Decimal) """
    rows, balance = [], Decimal('0')
    for entry in sorted(entries, key=_sort_key):
        bed, bes = Decimal(entry.get('bed') or 0), Decimal(entry.get('bes') or 0)
        balance += bed - bes
        kind, kind_label = classify(entry.get('comment'))
        rows.append({
            'ref': str(entry.get('ref') or ''), 'date': entry['date'], 'jalali': to_jalali(entry['date']),
            'comment': (entry.get('comment') or '').strip(), 'kind': kind, 'kind_label': kind_label,
            'bed': bed, 'bes': bes, 'balance': balance,
        })
    return rows


def to_jalali(value):
    return jdatetime.date.fromgregorian(date=value).strftime('%Y/%m/%d') if value else ''


def parse_jalali(text):
    """ «۱۴۰۵/۰۷/۰۱» یا «1405-7-1» ← datetime.date میلادی؛ ناخوانا ← None """
    text = to_latin_digits((text or '').strip()).replace('-', '/').replace('.', '/')
    parts = [p for p in text.split('/') if p]
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    year, month, day = (int(p) for p in parts)
    if year < 100:
        year += 1400
    try:
        return jdatetime.date(year, month, day).togregorian()
    except ValueError:
        return None


def preset_range(name, today=None):
    """ بازه‌ی آماده ← (از، تا) یا (None، None) برای «همه» """
    today = today or date.today()
    if name == '30':
        return today - timedelta(days=30), today
    if name == '90':
        return today - timedelta(days=90), today
    if name == 'year':
        jalali_today = jdatetime.date.fromgregorian(date=today)
        return jdatetime.date(jalali_today.year, 1, 1).togregorian(), today
    return None, None


def apply_range(rows, date_from=None, date_to=None):
    """ (ردیف‌های بازه، مانده‌ی ابتدای بازه) — مانده‌ی ابتدای بازه از آخرین ردیفِ پیش از آن می‌آید """
    opening = Decimal('0')
    selected = []
    for row in rows:
        if date_from and row['date'] < date_from:
            opening = row['balance']
            continue
        if date_to and row['date'] > date_to:
            continue
        selected.append(row)
    return selected, opening


def summarize(all_rows, selected, opening):
    total_bed = sum((r['bed'] for r in selected), Decimal('0'))
    total_bes = sum((r['bes'] for r in selected), Decimal('0'))
    current = all_rows[-1]['balance'] if all_rows else Decimal('0')
    return {
        'current_balance': current,
        'opening_balance': opening,
        'total_bed': total_bed,
        'total_bes': total_bes,
        'closing_balance': selected[-1]['balance'] if selected else opening,
        'count': len(selected),
    }


def balance_label(amount):
    """ (مقدار مطلق، «بدهکار» | «بستانکار» | «تسویه») برای نمایش به مشتری """
    if amount > 0:
        return abs(amount), 'بدهکار'
    if amount < 0:
        return abs(amount), 'بستانکار'
    return Decimal('0'), 'تسویه'


def _csv_safe(value):
    value = str(value)
    return "'" + value if value[:1] in ('=', '+', '-', '@') else value


def to_csv(rows):
    """ خروجی CSV (UTF-8 با BOM برای Excel فارسی)؛ ردیف‌ها به ترتیب نمایش (جدیدترین اول) """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(['تاریخ', 'شماره سند', 'نوع', 'شرح', 'بدهکار', 'بستانکار', 'مانده', 'وضعیت مانده'])
    for row in rows:
        amount, label = balance_label(row['balance'])
        writer.writerow([row['jalali'], row['ref'], row['kind_label'], _csv_safe(row['comment']),
                         f"{row['bed']:.0f}", f"{row['bes']:.0f}", f'{amount:.0f}', label])
    return '﻿' + buffer.getvalue()
