"""
تأمین‌کننده‌ی «معین حساب» مشتری برای accounts/ledger.py: کاردکس سرفصلِ بدهکارِ خودِ مشتری از هلو (فقط‌خواندنی).

منبع: GET /Report/GetKardexSarfasl?code=<BedSarfasl مشتری>&fa=true (آزمایش‌شده روی Holoo2؛ هر ردیف Code=شماره‌ی سند، Date میلادی
«YYYY/MM/DD»، Bed، Bes، Comment). کد سرفصل همیشه از رکورد خودِ کاربر (holoo_bed_sarfasl) می‌آید، نه از ورودی درخواست؛ پس
هیچ مشتری‌ای حساب دیگری را نمی‌بیند. گزارش «moien-report» وب‌سرویس در Holoo2 همیشه خالی برمی‌گرداند و استفاده نشد.
"""

import logging
from datetime import date
from decimal import Decimal, InvalidOperation

from .client import HolooClient

logger = logging.getLogger(__name__)

KARDEX_PATH = 'Report/GetKardexSarfasl'


def _decimal(value):
    try:
        return Decimal(str(value)).quantize(Decimal('1')) if value not in (None, '') else Decimal('0')
    except (InvalidOperation, ValueError):
        return Decimal('0')


def _parse_date(text):
    try:
        year, month, day = (int(part) for part in str(text).split('/'))
        return date(year, month, day)
    except (TypeError, ValueError):
        return None


def parse_kardex(data):
    """ پاسخ هلو ← فهرست ردیف‌های خنثی. ردیف بدون تاریخ معتبر کنار گذاشته می‌شود """
    rows = data.get('report') if isinstance(data, dict) else None
    if isinstance(rows, dict):          # یک ردیف تنها: شیء، نه فهرست
        rows = [rows]
    entries = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        when = _parse_date(row.get('Date'))
        if when is None:
            continue
        entries.append({'ref': str(row.get('Code') or ''), 'date': when, 'bed': _decimal(row.get('Bed')),
                        'bes': _decimal(row.get('Bes')), 'comment': str(row.get('Comment') or '').strip()})
    return entries


def fetch_customer_ledger(user):
    sarfasl = (getattr(user, 'holoo_bed_sarfasl', '') or '').strip()
    if not sarfasl:
        return {'status': 'not_linked', 'entries': []}
    client = HolooClient()
    data = client.get_json(KARDEX_PATH, params={'code': sarfasl, 'fa': 'true'}, timeout=max(client.config.timeout, 60))
    if data is None:                    # شبکه/لاگین/حالت mock
        return {'status': 'unavailable', 'entries': []}
    return {'status': 'ok', 'entries': parse_kardex(data)}
