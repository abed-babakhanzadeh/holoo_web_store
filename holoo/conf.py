"""
خواندن امن تنظیمات اتصال به هلو، بدون نیاز به ویرایش config/settings.py.

اولویت مقدارها (از بالا به پایین):
  ۱. متغیر محیطی پروسه (ویندوز / NSSM AppEnvironmentExtra)
  ۲. فایل `.env` در ریشه‌ی پروژه (در گیت نیست؛ پروسه‌های سایت، Celery و فرمان‌های مدیریتی همه یک‌جور می‌خوانند)
  ۳. نام‌های قدیمیِ settings.py (HOLOO_API_URL، HOLOO_USERNAME، ...)؛ فقط برای سازگاری با گذشته
  ۴. پیش‌فرض ایمن

دو پرچم کاملاً جدا:
  HOLOO_READ_MODE   = real | mock               (پیش‌فرض real؛ اگر HOLOO_PRODUCTS_MOCK_MODE قدیمی True بود mock)
  HOLOO_WRITE_MODE  = mock | disabled | real    (پیش‌فرض mock = شبیه‌سازی محلی؛ هیچ درخواست نوشتنی به هلو نمی‌رود)

نوشتن واقعی (ثبت مشتری و فاکتور) فقط با دو شرط هم‌زمان فعال است (وگرنه fail-closed ← disabled):
  ۱. HOLOO_WRITE_MODE=real صریحاً ست شده؛
  ۲. نام دیتابیس هلو در فهرست سفید HOLOO_WRITE_ALLOWED_DBS باشد (پیش‌فرض فقط `Holoo2`، دیتابیس آزمایشی)، و خواندن هم
     واقعی باشد. یعنی با یک env اشتباه، یا اشاره‌ی ناخواسته به دیتابیس دیگر، هرگز در هلوی دیگری چیزی نوشته نمی‌شود؛ برای
     دیتابیس عملیاتی باید نامش را عمداً به همین فهرست اضافه کرد.
هر مقدار نامعتبر برای پرچم نوشتن هم به disabled برمی‌گردد (غلط‌تایپی/env قدیمی خطر ندارد).

رمز هرگز در repr/لاگ/خروجی فرمان‌ها نمی‌آید.
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

READ_MODES = ('real', 'mock')
WRITE_MODES = ('mock', 'disabled', 'real')
DEFAULT_WRITE_ALLOWED_DBS = 'Holoo2'

DEFAULT_BASE_URL = 'http://127.0.0.1:8080/TncHoloo/api'
DEFAULT_TIMEOUT = 30


def _env_file_path():
    return Path(settings.BASE_DIR) / '.env'


def read_env_file(path=None):
    """ خواندن ساده‌ی KEY=VALUE از .env (بدون وابستگی تازه)؛ خط خالی/کامنت/بدون = نادیده گرفته می‌شود """
    path = Path(path) if path else _env_file_path()
    values = {}
    try:
        text = path.read_text(encoding='utf-8-sig')
    except (OSError, UnicodeDecodeError):
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip().removeprefix('export ').strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _truthy(value):
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


@dataclass(frozen=True)
class HolooConfig:
    base_url: str = DEFAULT_BASE_URL
    username: str = ''
    password: str = field(default='', repr=False)
    db_name: str = ''
    login_auth_header: str = '123'
    read_mode: str = 'real'
    write_mode: str = 'mock'
    write_allowed_dbs: tuple = ('Holoo2',)
    client_id_prefix: str = ''
    timeout: int = DEFAULT_TIMEOUT
    warnings: tuple = ()

    @property
    def read_is_mock(self):
        return self.read_mode == 'mock'

    @property
    def write_is_mock(self):
        return self.write_mode == 'mock'

    @property
    def write_is_disabled(self):
        return self.write_mode == 'disabled'

    @property
    def write_is_real(self):
        return self.write_mode == 'real'

    def problems(self):
        """ کمبودهایی که خواندن واقعی را ناممکن می‌کند (برای نمایش به مدیر؛ رمز را فاش نمی‌کند) """
        if self.read_is_mock:
            return []
        missing = [name for name, value in (
            ('HOLOO_API_URL', self.base_url), ('HOLOO_USERNAME', self.username),
            ('HOLOO_PASSWORD', self.password), ('HOLOO_DB_NAME', self.db_name),
        ) if not value]
        return [f'متغیر {name} تنظیم نشده است.' for name in missing]

    def masked(self):
        """ نمایش امن برای لاگ/فرمان: رمز فقط به‌صورت «تنظیم شده / نشده» """
        return {
            'base_url': self.base_url, 'username': self.username,
            'password': '***' if self.password else '(خالی)', 'db_name': self.db_name,
            'read_mode': self.read_mode, 'write_mode': self.write_mode, 'write_allowed_dbs': ','.join(self.write_allowed_dbs),
            'client_id_prefix': self.client_id_prefix, 'timeout': self.timeout,
        }


def get_config(env=None, env_file=None, settings_obj=None):
    """
    تنظیمات را (هر بار تازه، بدون کش) می‌سازد. پارامترها فقط برای تست‌اند.
    env: دیکشنری شبیه os.environ؛ env_file: مسیر .env؛ settings_obj: شیء شبیه django settings.
    """
    env = os.environ if env is None else env
    file_values = read_env_file(env_file)
    conf = settings if settings_obj is None else settings_obj
    warnings = []

    def pick(key, legacy=None, default=''):
        if env.get(key) not in (None, ''):
            return env[key]
        if file_values.get(key) not in (None, ''):
            return file_values[key]
        if legacy is not None:
            value = getattr(conf, legacy, None)
            if value not in (None, ''):
                return value
        return default

    read_mode = str(pick('HOLOO_READ_MODE', default='')).strip().lower()
    if not read_mode:
        legacy_mock = pick('HOLOO_PRODUCTS_MOCK_MODE', 'HOLOO_PRODUCTS_MOCK_MODE', 'False')
        read_mode = 'mock' if _truthy(legacy_mock) else 'real'
    if read_mode not in READ_MODES:
        warnings.append(f'HOLOO_READ_MODE نامعتبر ({read_mode!r}) بود؛ روی mock گذاشته شد.')
        read_mode = 'mock'

    write_mode = str(pick('HOLOO_WRITE_MODE', default='mock')).strip().lower()
    if write_mode not in WRITE_MODES:
        warnings.append(
            f'HOLOO_WRITE_MODE={write_mode!r} نامعتبر است؛ برای ایمنی روی disabled گذاشته شد '
            '(هیچ درخواست نوشتنی به هلو نمی‌رود).'
        )
        write_mode = 'disabled'

    allowed_raw = str(pick('HOLOO_WRITE_ALLOWED_DBS', default=DEFAULT_WRITE_ALLOWED_DBS))
    write_allowed_dbs = tuple(name.strip() for name in allowed_raw.split(',') if name.strip())
    db_name = str(pick('HOLOO_DB_NAME', 'HOLOO_DB_NAME'))
    if write_mode == 'real':
        if read_mode != 'real':
            warnings.append('HOLOO_WRITE_MODE=real با HOLOO_READ_MODE=mock سازگار نیست؛ نوشتن disabled شد.')
            write_mode = 'disabled'
        elif db_name.lower() not in {name.lower() for name in write_allowed_dbs}:
            warnings.append(
                f'نوشتن واقعی روی دیتابیس {db_name!r} مجاز نیست (فهرست سفید: {", ".join(write_allowed_dbs) or "خالی"}؛ '
                'HOLOO_WRITE_ALLOWED_DBS)؛ نوشتن disabled شد.'
            )
            write_mode = 'disabled'

    # id سمت کلاینت را هلو بدون نقل‌قول در SQL می‌گذارد (آزمایش: «MM-1» ← «Invalid column name 'MM'»)؛ پس پیشوند فقط رقم باشد
    client_id_prefix = str(pick('HOLOO_CLIENT_ID_PREFIX', default='')).strip()
    if client_id_prefix and not client_id_prefix.isdigit():
        warnings.append(
            f'HOLOO_CLIENT_ID_PREFIX={client_id_prefix!r} نامعتبر است (فقط رقم مجاز است؛ هلو id را عددی می‌پذیرد، مثلاً 8800)؛ '
            'برای ایمنی نوشتن disabled شد.'
        )
        write_mode = 'disabled'

    try:
        timeout = int(pick('HOLOO_TIMEOUT', default=DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT

    config = HolooConfig(
        base_url=str(pick('HOLOO_API_URL', 'HOLOO_API_URL', DEFAULT_BASE_URL)).rstrip('/'),
        username=str(pick('HOLOO_USERNAME', 'HOLOO_USERNAME')),
        password=str(pick('HOLOO_PASSWORD', 'HOLOO_PASSWORD')),
        db_name=db_name,
        login_auth_header=str(pick('HOLOO_LOGIN_AUTH_HEADER', 'HOLOO_LOGIN_AUTH_HEADER', '123')),
        read_mode=read_mode, write_mode=write_mode, write_allowed_dbs=write_allowed_dbs,
        client_id_prefix=client_id_prefix,
        timeout=max(timeout, 1), warnings=tuple(warnings),
    )
    for message in warnings:
        logger.warning(message)
    return config
