"""
حساب‌های بانکی هلو (GET /Payment/Account) برای انتخاب «سرفصل کارتخوان» در تنظیمات سایت.

هلو فاکتور پرداخت‌شده را فقط با «کد سرفصل» حساب دارای کارتخوان (hasPOS=true) می‌پذیرد، مثلاً 10200010004. شماره‌ی حساب
بانکی (0110062796003) یا هر مقدار دیگری خطای ۳۲ «سرفصل کارت‌خوان معتبر نمی‌باشد» می‌دهد؛ ولی در ادمین هلو و کارت بانکی
فقط «شماره حساب» دیده می‌شود، پس مدیر طبیعی است همان را وارد کند (اتفاقِ ۱۳ اکتبر ۲۰۲۶). این ماژول:
  - حساب‌ها را از هلو می‌خواند و برای ادمین فهرست قابل‌انتخاب می‌سازد،
  - مقدار واردشده (کد سرفصل یا شماره حساب) را به کد سرفصل درست ترجمه/راستی‌آزمایی می‌کند.
"""

import logging
import re

from services.text import to_latin_digits

from .wire import normalize_fa

logger = logging.getLogger(__name__)

ACCOUNTS_PATH = 'Payment/Account'

# نتیجه‌ی match_account
BY_SARFASL = 'sarfasl'
BY_ACCOUNT_NUMBER = 'account_number'


def clean_code(value):
    """ مقدار واردشده‌ی مدیر → فقط رقم‌های لاتین (فاصله/خط‌تیره/ارقام فارسی پاک یا تبدیل می‌شوند) """
    return re.sub(r'[\s\-_/]', '', to_latin_digits(str(value if value is not None else '')))


def list_bank_accounts(client=None, timeout=None):
    """
    فهرست حساب‌های بانکی هلو یا None وقتی خواندن ممکن نیست (حالت mock، خطای شبکه/لاگین یا پاسخ نامعتبر).
    هر ردیف: sarfasl، bank، branch، account_number، card_number، has_pos.
    """
    if client is None:
        from .client import HolooClient
        client = HolooClient()
    data = client.get_json(ACCOUNTS_PATH, timeout=timeout)
    if not isinstance(data, list):
        return None
    accounts = []
    for row in data:
        if not isinstance(row, dict) or not row.get('sarfaslCode'):
            continue
        accounts.append({
            'sarfasl': clean_code(row.get('sarfaslCode')),
            'bank': normalize_fa(str(row.get('bankName') or '')).strip(),
            'branch': normalize_fa(str(row.get('branchName') or '')).strip(),
            'account_number': clean_code(row.get('accountNumber')),
            'card_number': clean_code(row.get('cardNumber')),
            'has_pos': bool(row.get('hasPOS')),
        })
    return accounts


def match_account(value, accounts):
    """
    (حساب، روش تطبیق) یا (None, None). اول با «کد سرفصل» تطبیق می‌دهد، بعد با «شماره حساب» (همان اشتباه رایج). شماره‌ی
    حسابی که در چند حساب تکراری باشد مبهم است و تطبیق نمی‌خورد (به‌جای حدس زدن).
    """
    code = clean_code(value)
    if not code:
        return None, None
    for account in accounts or ():
        if account['sarfasl'] == code:
            return account, BY_SARFASL
    same_number = [account for account in accounts or () if account['account_number'] and account['account_number'] == code]
    if len(same_number) == 1:
        return same_number[0], BY_ACCOUNT_NUMBER
    return None, None


def account_label(account):
    """ «ملی — شعبه‌ی عمارياسر — حساب 0110062796003 (سرفصل 10200010004)» برای فهرست انتخاب ادمین """
    parts = [account['bank'] or 'بانک نامشخص']
    if account['branch']:
        parts.append(f"شعبه‌ی {account['branch']}")
    if account['account_number']:
        parts.append(f"حساب {account['account_number']}")
    return f"{' — '.join(parts)} (سرفصل {account['sarfasl']})"


def pos_accounts(accounts):
    return [account for account in accounts or () if account['has_pos']]


def resolve_pos_sarfasl(value, client=None):
    """
    مقدار تنظیم‌شده را به کد سرفصلِ معتبرِ کارتخوان ترجمه می‌کند، یا None اگر نشد (هلو در دسترس نیست، حساب پیدا نشد،
    یا حساب «متصل به کارت‌خوان» نیست). فقط برای بازیابی بعد از خطای ۳۲ استفاده می‌شود، نه هر فاکتور.
    """
    try:
        accounts = list_bank_accounts(client)
    except Exception:                                        # noqa: BLE001 - بازیابی نباید خودش تسک را بشکند
        logger.exception("خواندن حساب‌های بانکی هلو برای اصلاح سرفصل کارتخوان ناموفق بود.")
        return None
    if accounts is None:
        return None
    account, _how = match_account(value, accounts)
    if account is None or not account['has_pos']:
        return None
    return account['sarfasl']
