"""
ترجمه‌ی بین ساختار خنثی‌ی داخلی پروژه و قرارداد واقعی وب‌سرویس هلو (TncHoloo 1.7.33؛ آزمایش‌شده روی Holoo2).

بقیه‌ی پروژه (holoo/invoice.py، تسک‌ها) با دیکشنری‌های ساده‌ی خودشان کار می‌کنند و از شکل بدنه‌ی هلو بی‌خبرند؛ فقط این ماژول
(و کلاینت) شکل واقعی را می‌شناسد. اگر هلو عوض شود، این‌جا و کلاینت عوض می‌شوند، نه سفارش/پرداخت.

نکته‌های قرارداد (نتیجه‌ی آزمایش میدانی، ۴ اکتبر ۲۰۲۶):
  - همه‌ی پاسخ‌ها HTTP 200 هستند؛ موفقیت {"Success": {...}} و شکست {"Failure": {"Error", "ErrorCode", ...}}.
  - `id` سمت کلاینتِ فاکتور یکتاست (خطای ۱۰۲ در تکرار)؛ برای مشتری و سند دریافت یکتا نیست.
  - تسویه‌ی فاکتور باید دقیقاً با جمع آن برابر باشد (خطای ۳۶): پرداخت‌شده ← `Bank` با سرفصل کارتخوان، وگرنه `Nesiyeh`.
  - مالیات و عوارض صفر فرستاده می‌شود (قیمت‌های سایت نهایی‌اند؛ تصمیم مدیریت).
"""

import re
from decimal import Decimal

# کدهای خطایی که به «در دسترس نبودن/آماده نبودن هلو» برمی‌گردند و تلاش دوباره معنی دارد
TRANSIENT_CODES = frozenset({'1', '3', '4', '5'})
CODE_NO_STOCK = '28'
CODE_DUPLICATE_CLIENT_ID = '102'
CODES_EXISTING_CUSTOMER = frozenset({'10', '23'})       # موبایل / کدملی تکراری؛ هلو ErpCode مشتریِ موجود را هم برمی‌گرداند

COMMENT_MAX_LENGTH = 450

# دیتابیس هلو (SQL Server با کدپیج عربی/فارسی ویندوز) ارقام فارسی/عربی-هندی را ذخیره نمی‌کند و به «?» تبدیل می‌کند
# (آزمایش میدانی: «پلاک ۱» ← «پلاک ?»). پس همه‌ی متن‌های ارسالی با ارقام لاتین نوشته می‌شوند.
_DIGIT_TABLE = str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')


def holoo_text(value):
    """ متن امن برای ذخیره در هلو: ارقام فارسی و عربی ← لاتین (None ← رشته‌ی خالی) """
    return str(value if value is not None else '').translate(_DIGIT_TABLE)


def normalize_fa(text):
    """ هلو متن فارسی را با «ي/ك» عربی ذخیره می‌کند؛ برای مقایسه‌ی کامنت‌ها هر دو طرف یکدست می‌شوند """
    return (text or '').replace('ي', 'ی').replace('ك', 'ک')


def number(value):
    """ عدد برای JSON: صحیح اگر صحیح است (۶۶۷۰۰ نه ۶۶۷۰۰٫۰) """
    value = Decimal(str(value))
    return int(value) if value == value.to_integral_value() else float(value)


def customer_display_name(first_name, last_name, phone_number):
    """ «نام نام‌خانوادگی – موبایل» (تصمیم مدیریت): حتی دو «علی احمدی» در هلو از هم قابل‌تفکیک‌اند """
    full = ' '.join(part.strip() for part in (first_name, last_name) if part and part.strip())
    return holoo_text(f'{full} – {phone_number}' if full else str(phone_number))


def customer_body(*, web_id, first_name, last_name, phone_number, national_code='', province='', city='', address='',
                  postal_code='', email=''):
    """ بدنه‌ی POST /Customer؛ خریدار، نه فروشنده، نوع بدهکار (custtype=0) """
    return {'custinfo': [{
        'id': str(web_id),
        'name': customer_display_name(first_name, last_name, phone_number),
        'ispurchaser': True, 'isseller': False, 'custtype': 0, 'type': 1,
        'mobile': holoo_text(phone_number), 'nationalid': holoo_text(national_code),
        'ostan': holoo_text(province), 'city': holoo_text(city), 'address': holoo_text(address), 'zipcode': holoo_text(postal_code),
        'email': holoo_text(email),
    }]}


def customer_update_body(erp_code, *, web_id=None, first_name=None, last_name=None, phone_number=None, national_code=None,
                         province=None, city=None, address=None, postal_code=None, email=None):
    """ بدنه‌ی PUT /Customer؛ فقط فیلدهای داده‌شده (None = دست‌نخورده). id را باید دوباره فرستاد وگرنه WebId پاک می‌شود. """
    info = {'erpcode': erp_code}
    if web_id is not None:
        info['id'] = str(web_id)
    if first_name is not None or last_name is not None:
        info['name'] = customer_display_name(first_name, last_name, phone_number)
    for key, value in (('mobile', phone_number), ('nationalid', national_code), ('ostan', province), ('city', city),
                       ('address', address), ('zipcode', postal_code), ('email', email)):
        if value is not None:
            info[key] = holoo_text(value)
    return {'custinfo': [info]}


def invoice_body(payload, id_prefix=''):
    """
    بدنه‌ی POST /Invoice/Invoice از روی ساختار خنثی (holoo/invoice.py::build_invoice_payload):
      OrderId ← id یکتا (idempotency)، IssuedAt ← تاریخ/ساعت میلادی، Items ← detailinfo (فهرست)،
      Paid/PosSarfasl ← تسویه‌ی کارتخوان یا نسیه (مبلغ تسویه همیشه جمع ردیف‌هاست تا خطای ۳۶ نیاید).
    """
    from .invoice import payload_total

    issued = payload['IssuedAt']
    total = payload_total(payload)
    info = {
        'id': f"{id_prefix}{payload['OrderId']}",
        'type': 1,
        'customererpcode': payload['CustomerErpCode'],
        'date': issued.strftime('%Y-%m-%d'),
        'time': issued.strftime('%H:%M'),
        'comment': holoo_text(payload.get('Comment'))[:COMMENT_MAX_LENGTH],
    }
    if payload.get('Paid'):
        info['Bank'] = number(total)
        info['BankSarfasl'] = payload['PosSarfasl']
    else:
        info['Nesiyeh'] = number(total)
    info['detailinfo'] = [
        {
            'id': str(index),
            'ProductErpCode': row['ErpCode'],
            'few': number(row['Amount']),
            'price': number(row['Price']),
            'levy': 0, 'scot': 0,
            'comment': holoo_text(row.get('Comment'))[:200],
        }
        for index, row in enumerate(payload['Items'], start=1)
    ]
    return {'invoiceinfo': [info]}


def parse_response(data):
    """
    تفسیر پاسخ هلو → دیکشنری یکدست:
      {'success': True, 'data': {...Success}}
      {'success': False, 'code': '28', 'message': '...', 'transient': False, 'failure': {...}}
    پاسخی که نه Success دارد نه Failure (خالی/متن/ساختار ناشناخته) «موقت» حساب می‌شود (احتمالاً نیمه‌کاره بودن سرویس).
    """
    if isinstance(data, dict):
        if isinstance(data.get('Success'), dict):
            return {'success': True, 'data': data['Success']}
        failure = data.get('Failure')
        if isinstance(failure, dict):
            code = str(failure.get('ErrorCode', ''))
            return {
                'success': False, 'code': code, 'message': str(failure.get('Error') or 'خطای نامشخص هلو'),
                'transient': code in TRANSIENT_CODES, 'failure': failure,
            }
    return {'success': False, 'code': 'BAD_RESPONSE', 'message': f'پاسخ قابل‌تفسیر نبود: {str(data)[:200]}',
            'transient': True, 'failure': {}}


def comment_mentions_order(comment, order_id):
    """ آیا کامنتِ فاکتور هلو به «سفارش آنلاین سایت کد #<order_id>» اشاره می‌کند (با یکدست‌سازی ی/ک) """
    text = normalize_fa(comment)
    return re.search(rf'سفارش\s+آنلاین\s+سایت\s+کد\s*#{int(order_id)}(?!\d)', text) is not None
