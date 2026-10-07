"""
منطق خالص (بدون وابستگی به اپ chat و بدون دسترسی به دیتابیس) برای تنظیمات گفتگوی آنلاین در SiteSettings.

همه‌ی چیزهایی که مدیر در تب «گفتگوی آنلاین» می‌نویسد اینجا تحلیل و اعتبارسنجی می‌شود:
  - ساعات کاری هر روز هفته («08:00-12:00, 13:00-17:00»)، تعطیلات جلالی، منطقه‌ی زمانی و وضعیت «در ساعت کاری»
  - الگوی صفحات مستثنی (مسیر دقیق، پیشوند با «*»، یا «name:نام_url»)
  - متن‌های حباب، رنگ، بازه‌ی اعداد

این فایل عمداً به هیچ مدل/اپ دیگری import نمی‌دهد تا وابستگی فقط یک‌طرفه باشد: chat → products.
"""
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import jdatetime

from services.text import to_latin_digits

CHAT_CFG_CACHE_KEY = 'chat:cfg'
CHAT_CFG_CACHE_TTL = 300
CHAT_ASSET_VERSION = '2'       # با هر تغییر در chat-widget.js/css عوض شود تا کش مرورگر کهنه نماند

# هفته‌ی ایرانی: شنبه تا جمعه. کلید = weekday() پایتون (دوشنبه=۰ ... یکشنبه=۶)
WEEK = (
    (5, 'sat', 'شنبه'), (6, 'sun', 'یکشنبه'), (0, 'mon', 'دوشنبه'), (1, 'tue', 'سه‌شنبه'),
    (2, 'wed', 'چهارشنبه'), (3, 'thu', 'پنجشنبه'), (4, 'fri', 'جمعه'),
)
WEEKDAY_FIELD = {weekday: f'chat_hours_{key}' for weekday, key, _ in WEEK}
WEEKDAY_NAME = {weekday: name for weekday, _, name in WEEK}

MAX_EXCLUDED_LINES = 50
MAX_BUBBLE_LINES = 20
MAX_BUBBLE_LENGTH = 120

_PERSIAN_DIGITS = '۰۱۲۳۴۵۶۷۸۹'


def to_persian_digits(text):
    return ''.join(_PERSIAN_DIGITS[int(c)] if c.isdigit() and c.isascii() else c for c in str(text))


def clamp(value, low, high, default=None):
    """ عدد را در بازه‌ی [low, high] نگه می‌دارد؛ مقدار نامعتبر ← default (یا low) """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default if default is not None else low
    return max(low, min(high, number))


# ------------------------------------------------------------------ ساعات کاری

_RANGE_RE = re.compile(r'^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$')


def _minutes(hour, minute):
    hour, minute = int(hour), int(minute)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError('ساعت باید بین 00:00 تا 23:59 باشد.')
    return hour * 60 + minute


def parse_hours_line(text):
    """
    «08:00-12:00, 13:00-17:00» ← [(480, 720), (780, 1020)] (دقیقه از نیمه‌شب). خالی = تعطیل ← [].
    جداکننده ویرگول لاتین یا فارسی؛ ارقام فارسی هم قبول است. شروع < پایان؛ بازه‌ها همپوشانی ندارند؛
    عبور از نیمه‌شب مجاز نیست (دو بازه بنویسید). خطا ← ValueError با پیام فارسی.
    """
    cleaned = to_latin_digits((text or '').strip()).replace('،', ',').replace('–', '-').replace('—', '-')
    if not cleaned:
        return []
    ranges = []
    for part in (p.strip() for p in cleaned.split(',')):
        if not part:
            continue
        match = _RANGE_RE.match(part)
        if not match:
            raise ValueError(f'«{part}» درست نیست؛ قالب صحیح: 08:00-12:00')
        start, end = _minutes(match.group(1), match.group(2)), _minutes(match.group(3), match.group(4))
        if start >= end:
            raise ValueError(f'در «{part}» ساعت شروع باید قبل از پایان باشد (برای عبور از نیمه‌شب دو بازه بنویسید).')
        ranges.append((start, end))
    ranges.sort()
    for (_, previous_end), (next_start, _) in zip(ranges, ranges[1:]):
        if next_start < previous_end:
            raise ValueError('بازه‌های ساعتی یک روز نباید همپوشانی داشته باشند.')
    return ranges


def format_minutes(value):
    return f'{value // 60:02d}:{value % 60:02d}'


def format_ranges(ranges):
    """ [(480, 720), (780, 1020)] ← «۰۸:۰۰ تا ۱۲:۰۰ و ۱۳:۰۰ تا ۱۷:۰۰» """
    return to_persian_digits(' و '.join(f'{format_minutes(a)} تا {format_minutes(b)}' for a, b in ranges))


def parse_holidays(text):
    """
    هر خط: «1405/07/21 عنوان اختیاری» (تاریخ جلالی) ← {date میلادی: عنوان}. خط خالی و خطی که با # شروع شود نادیده است.
    خطا ← ValueError با شماره‌ی خط.
    """
    holidays = {}
    for number, raw in enumerate((text or '').splitlines(), start=1):
        line = to_latin_digits(raw.strip())
        if not line or line.startswith('#'):
            continue
        match = re.match(r'^(\d{4})[/-](\d{1,2})[/-](\d{1,2})\s*(.*)$', line)
        if not match:
            raise ValueError(f'خط {number}: قالب تاریخ باید مثل 1405/07/21 باشد.')
        try:
            gregorian = jdatetime.date(int(match.group(1)), int(match.group(2)), int(match.group(3))).togregorian()
        except ValueError:
            raise ValueError(f'خط {number}: این تاریخ جلالی وجود ندارد.') from None
        holidays[gregorian] = match.group(4).strip()
    return holidays


def validate_timezone(name):
    try:
        ZoneInfo((name or '').strip())
    except (ZoneInfoNotFoundError, ValueError, KeyError, OSError):
        raise ValueError('نام منطقه‌ی زمانی معتبر نیست (مثال: Asia/Tehran).') from None


def weekly_schedule(values):
    """ values: دیکشنری {weekday پایتون: متن ساعت‌ها} ← {weekday: [(start, end)]} """
    return {weekday: parse_hours_line(values.get(weekday, '')) for weekday, _, _ in WEEK}


def working_state(mode, schedule, holidays, tz_name, now):
    """
    وضعیت ساعت کاری در لحظه‌ی now (datetime آگاه یا UTC).
      mode == 'always'     ← همیشه در ساعت کاری
      mode == 'by_schedule' ← طبق روز هفته، بازه‌ها و تعطیلات (تاریخ به وقت منطقه‌ی چت)
    خروجی: {in_hours, today_ranges, holiday_title, today_label, next_open_label}
    """
    zone = ZoneInfo(tz_name)
    local = now.astimezone(zone) if now.tzinfo else now.replace(tzinfo=zone)
    if mode == 'always':
        return {'in_hours': True, 'today_ranges': [(0, 24 * 60)], 'holiday_title': '', 'today_label': 'همیشه',
                'next_open_label': ''}

    today = local.date()
    minute = local.hour * 60 + local.minute
    is_holiday = today in holidays
    today_ranges = [] if is_holiday else schedule.get(today.weekday(), [])
    in_hours = any(start <= minute < end for start, end in today_ranges)

    if is_holiday:
        label = 'امروز تعطیل است' + (f' ({holidays[today]})' if holidays[today] else '')
    elif today_ranges:
        label = 'ساعات پاسخگویی امروز: ' + format_ranges(today_ranges)
    else:
        label = 'امروز تعطیل است'
    return {'in_hours': in_hours, 'today_ranges': today_ranges, 'holiday_title': holidays.get(today, ''),
            'today_label': label, 'next_open_label': _next_open_label(schedule, holidays, local)}


def _next_open_label(schedule, holidays, local):
    """ «امروز ساعت ۱۳:۰۰» / «فردا ساعت ۰۸:۰۰» / «شنبه ساعت ۰۸:۰۰»؛ خالی اگر تا ۸ روز آینده بازه‌ای نیست """
    minute = local.hour * 60 + local.minute
    for offset in range(0, 9):
        day = local.date() + timedelta(days=offset)
        if day in holidays:
            continue
        for start, end in schedule.get(day.weekday(), []):
            if offset == 0 and start <= minute:
                continue          # امروز و شروعش گذشته (یا همین الان در آن هستیم)
            when = 'امروز' if offset == 0 else 'فردا' if offset == 1 else WEEKDAY_NAME[day.weekday()]
            return to_persian_digits(f'{when} ساعت {format_minutes(start)}')
    return ''


# ------------------------------------------------------------------ صفحات مستثنی

_NAME_RE = re.compile(r'^name:[A-Za-z0-9_.:-]+$')


def parse_excluded_paths(text):
    """
    هر خط یکی از این‌ها (فقط glob ساده؛ بدون regex تا خطر ReDoS نباشد):
      /orders/checkout/      مسیر دقیق (با/بی‌ تکیه بر «/» پایانی)
      /payments/*            پیشوند («*» فقط انتهای الگو)
      name:orders:checkout   نام URL (namespace:name)؛ مقاوم در برابر تغییر آدرس‌ها
    ← [('exact'|'prefix'|'name', مقدار)]. خطا ← ValueError.
    """
    patterns = []
    lines = [line.strip() for line in (text or '').splitlines() if line.strip() and not line.strip().startswith('#')]
    if len(lines) > MAX_EXCLUDED_LINES:
        raise ValueError(f'حداکثر {MAX_EXCLUDED_LINES} الگو مجاز است.')
    for line in lines:
        if line.startswith('name:'):
            if not _NAME_RE.match(line):
                raise ValueError(f'«{line}» درست نیست؛ مثال: name:orders:checkout')
            patterns.append(('name', line[5:]))
        elif line.startswith('/'):
            if '*' in line[:-1] or any(c.isspace() for c in line) or '?' in line:
                raise ValueError(f'«{line}» درست نیست؛ «*» فقط می‌تواند آخر الگو باشد و فاصله/؟ مجاز نیست.')
            patterns.append(('prefix', line[:-1]) if line.endswith('*') else ('exact', line))
        else:
            raise ValueError(f'«{line}» باید با «/» یا «name:» شروع شود.')
    return patterns


def path_is_excluded(patterns, path, url_name=''):
    path = path or '/'
    for kind, value in patterns:
        if kind == 'prefix' and path.startswith(value):
            return True
        if kind == 'exact' and (path == value or path.rstrip('/') == value.rstrip('/')):
            return True
        if kind == 'name' and url_name and url_name == value:
            return True
    return False


# ------------------------------------------------------------------ متن‌ها

def parse_bubble_lines(text):
    """ هر خط یک حباب؛ حداکثر ۲۰ خط و ۱۲۰ نویسه ← فهرست متن‌ها. خطا ← ValueError """
    lines = [line.strip() for line in (text or '').splitlines() if line.strip()]
    if len(lines) > MAX_BUBBLE_LINES:
        raise ValueError(f'حداکثر {MAX_BUBBLE_LINES} حباب مجاز است.')
    for line in lines:
        if len(line) > MAX_BUBBLE_LENGTH:
            raise ValueError(f'هر حباب حداکثر {MAX_BUBBLE_LENGTH} نویسه است («{line[:20]}…» بلندتر است).')
    return lines


def parse_phone_list(text):
    """ شماره‌های موبایل (هر خط یکی) ← فهرست 09xxxxxxxxx؛ ارقام فارسی مجاز. خطا ← ValueError """
    phones = []
    for line in (text or '').splitlines():
        line = to_latin_digits(line.strip())
        if not line:
            continue
        if not re.fullmatch(r'09\d{9}', line):
            raise ValueError(f'«{line}» شماره‌ی موبایل معتبر نیست (قالب 09123456789).')
        phones.append(line)
    return phones


# ------------------------------------------------------------------ نمایش و دسترسی

def viewer_allowed(settings_obj, is_authenticated):
    return bool(settings_obj.chat_visible_for_users if is_authenticated else settings_obj.chat_visible_for_guests)


def visible_tabs(settings_obj):
    """ زبانه‌هایی که باید در ویجت باشند: [(کلید، برچسب، coming_soon)] """
    tabs = []
    if settings_obj.chat_tab_live_enabled:
        tabs.append(('live', settings_obj.chat_tab_live_label, False))
    if settings_obj.chat_tab_offline_enabled:
        tabs.append(('offline', settings_obj.chat_tab_offline_label, False))
    if settings_obj.chat_ai_tab_mode == 'coming_soon':
        tabs.append(('ai', settings_obj.chat_tab_ai_label, True))
    return tabs


def widget_should_render(settings_obj, *, is_authenticated, path, url_name=''):
    """
    آیا ویجت برای این درخواست اصلاً رندر شود؟ (تصمیم سمت سرور؛ اگر False باشد هیچ HTML/اسکریپتی نمی‌آید)
    """
    if not settings_obj.chat_enabled or not viewer_allowed(settings_obj, is_authenticated):
        return False
    if not visible_tabs(settings_obj):
        return False
    try:
        patterns = parse_excluded_paths(settings_obj.chat_excluded_paths)
    except ValueError:
        patterns = []         # الگوی خراب (مثلاً با نوشتن مستقیم در DB) ویجت را نمی‌شکند
    return not path_is_excluded(patterns, path, url_name)


# ------------------------------------------------------------------ اعتبارسنجی SiteSettings

COLOR_RE = re.compile(r'^#[0-9A-Fa-f]{6}$')


def validate_chat_settings(obj):
    """ خطاهای فرم ادمین برای فیلدهای chat_* ← {فیلد: پیام}؛ از SiteSettings.clean صدا زده می‌شود """
    errors = {}

    def check(field, func):
        try:
            func(getattr(obj, field))
        except ValueError as error:
            errors[field] = str(error)

    check('chat_timezone', validate_timezone)
    check('chat_excluded_paths', parse_excluded_paths)
    check('chat_bubble_messages', parse_bubble_lines)
    check('chat_holidays', parse_holidays)
    check('chat_notify_phones', parse_phone_list)
    for _, key, _name in WEEK:
        check(f'chat_hours_{key}', parse_hours_line)
    if not COLOR_RE.match(obj.chat_primary_color or ''):
        errors['chat_primary_color'] = 'رنگ باید کد Hex شش‌رقمی مثل #FF8229 باشد.'
    if obj.chat_poll_closed_seconds not in (0,) and not 15 <= obj.chat_poll_closed_seconds <= 600:
        errors['chat_poll_closed_seconds'] = 'مقدار باید ۰ (بدون پولینگ) یا بین ۱۵ تا ۶۰۰ ثانیه باشد.'
    if obj.chat_poll_idle_seconds < obj.chat_poll_active_seconds:
        errors['chat_poll_idle_seconds'] = 'فاصله‌ی حالت بی‌فعالیت نباید کمتر از حالت فعال باشد.'
    if obj.chat_customer_gone_minutes <= obj.chat_customer_idle_minutes:
        errors['chat_customer_gone_minutes'] = 'باید بیشتر از «زمان بی‌پاسخی مشتری» باشد.'
    if obj.chat_hours_mode == 'by_schedule':
        try:
            schedule = weekly_schedule({weekday: getattr(obj, field) for weekday, field in WEEKDAY_FIELD.items()})
        except ValueError:
            schedule = None                      # خطای هر روز بالاتر گزارش شده
        if schedule is not None and not any(schedule.values()):
            errors['chat_hours_mode'] = ('در حالت «طبق برنامه» حداقل یک روز هفته باید ساعت کاری داشته باشد '
                                         '(یا حالت «همیشه» را انتخاب کنید).')
    return errors


def week_schedule_from_settings(obj):
    """ برنامه‌ی هفتگی از فیلدهای SiteSettings؛ روز خراب (نوشتن مستقیم در DB) تعطیل حساب می‌شود """
    schedule = {}
    for weekday, field in WEEKDAY_FIELD.items():
        try:
            schedule[weekday] = parse_hours_line(getattr(obj, field))
        except ValueError:
            schedule[weekday] = []
    return schedule


def holidays_from_settings(obj):
    try:
        return parse_holidays(obj.chat_holidays)
    except ValueError:
        return {}


def current_hours_state(obj, now):
    """ وضعیت ساعت کاری فعلی بر اساس تنظیمات؛ منطقه‌ی زمانی نامعتبر ← Asia/Tehran """
    tz_name = (obj.chat_timezone or '').strip()
    try:
        validate_timezone(tz_name)
    except ValueError:
        tz_name = 'Asia/Tehran'
    return working_state(obj.chat_hours_mode, week_schedule_from_settings(obj), holidays_from_settings(obj), tz_name, now)
