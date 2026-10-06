"""
ورود گروهی مشتریان هلو به سایت (فقط خواندن از هلو؛ نوشتن فقط در دیتابیس سایت).

هر مشتریِ خریدارِ فعالِ هلو که موبایل معتبر دارد یک کاربر سایت می‌شود، با کد هلو و سطح قیمتِ خودش. کاربر رمز ندارد (ورود با
کد یکبارمصرف)؛ اولین بار که وارد شد نامش (از هلو پر شده) و کدملی را کامل می‌کند و همان لحظه خودکار تأیید می‌شود
(CustomUser.approve_if_trusted_import). هلو مالک این مشتری می‌ماند: سایت نام/آدرسش را بازنویسی نمی‌کند (holoo/tasks.py::sync_customer).

قواعد (طبق تصمیم کارفرما):
  - فقط IsPurchaser، IsActive و غیر لیست‌سیاه؛ تأمین‌کننده‌ها و حساب‌های غیرفعال وارد نمی‌شوند.
  - موبایل باید قابل نرمال‌سازی به 09XXXXXXXXX باشد (ردیف بدون موبایل/نامعتبر فقط گزارش می‌شود).
  - ردیفی که WebId دارد را خود سایت (یا یک تست) ساخته؛ آن کاربرها از قبل کد هلو دارند و دوباره ساخته نمی‌شوند.
  - اگر چند مشتری یک موبایل دارند، کوچک‌ترین «کد» انتخاب می‌شود و بقیه در گزارش می‌آیند (برای تصمیم دستی).
  - اجرای دوباره بی‌خطر است (idempotent): فقط مشتریان تازه‌ی هلو اضافه می‌شوند.
"""
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from django.db import transaction

from accounts.models import ApprovalStatus, CustomUser, UserStatus, normalize_phone_number

REASON_LABELS = {
    'not_purchaser': 'خریدار نیست (تأمین‌کننده)',
    'inactive': 'حساب غیرفعال در هلو',
    'blacklisted': 'لیست سیاه هلو',
    'no_erp': 'بدون شناسه‌ی هلو',
    'no_mobile': 'بدون موبایل',
    'invalid_mobile': 'موبایل نامعتبر',
    'site_origin': 'ساخته‌شده توسط سایت (WebId دارد)',
    'shared_mobile': 'موبایل مشترک با مشتری دیگر (انتخاب نشد)',
}

_ARABIC_TO_PERSIAN = str.maketrans({'ي': 'ی', 'ك': 'ک', 'ى': 'ی', 'ۀ': 'ه', 'ة': 'ه'})


def clean_name(name):
    """ نام هلو برای نمایش: حروف عربی ← فارسی، فاصله‌های اضافه حذف """
    return re.sub(r'\s+', ' ', (name or '').translate(_ARABIC_TO_PERSIAN)).strip()


def split_name(name):
    """ نام تک‌فیلدی هلو ← (نام، نام‌خانوادگی)؛ آخرین کلمه نام‌خانوادگی است. فقط پیش‌فرضِ قابل‌ویرایش برای فرم پروفایل. """
    parts = clean_name(name).split(' ')
    parts = [part for part in parts if part]
    if not parts:
        return '', ''
    if len(parts) == 1:
        return parts[0][:50], ''
    return ' '.join(parts[:-1])[:50], parts[-1][:50]


def price_level_of(row):
    """ سطح قیمت مشتری در هلو (selectedPriceType) ← price_level سایت؛ ۰/نامعتبر ← ۱ """
    try:
        level = int(row.get('selectedPriceType') or 1)
    except (TypeError, ValueError):
        level = 1
    return level if level in CustomUser.valid_price_levels() else 1


def _code_key(row):
    try:
        return int(row.get('Code'))
    except (TypeError, ValueError):
        return 10 ** 12


def _has_webid(row):
    return str(row.get('WebId') or '').strip().lower() not in ('', 'null', 'none')


@dataclass
class Plan:
    candidates: list = field(default_factory=list)     # [(mobile, row)] برای ورود
    skipped: list = field(default_factory=list)        # [(row, reason, detail)]

    def reason_counts(self):
        return Counter(reason for _, reason, _ in self.skipped)


def build_plan(rows):
    """ از ردیف‌های خام هلو برنامه‌ی ورود می‌سازد (خالص؛ بدون دسترسی به دیتابیس) """
    plan = Plan()
    by_mobile = defaultdict(list)
    for row in rows:
        if not row.get('ErpCode'):
            plan.skipped.append((row, 'no_erp', ''))
        elif not row.get('IsPurchaser'):
            plan.skipped.append((row, 'not_purchaser', ''))
        elif not row.get('IsActive', True):
            plan.skipped.append((row, 'inactive', ''))
        elif row.get('IsBlackList'):
            plan.skipped.append((row, 'blacklisted', ''))
        elif _has_webid(row):
            plan.skipped.append((row, 'site_origin', ''))
        elif not (row.get('Mobile') or '').strip():
            plan.skipped.append((row, 'no_mobile', ''))
        else:
            try:
                by_mobile[normalize_phone_number(row['Mobile'])].append(row)
            except ValueError:
                plan.skipped.append((row, 'invalid_mobile', row.get('Mobile')))

    for mobile, group in by_mobile.items():
        group.sort(key=_code_key)
        winner, losers = group[0], group[1:]
        plan.candidates.append((mobile, winner))
        for row in losers:
            plan.skipped.append((row, 'shared_mobile', f"انتخاب‌شده: کد {winner.get('Code')} ({clean_name(winner.get('Name'))})"))
    plan.candidates.sort(key=lambda item: _code_key(item[1]))
    return plan


def fetch_customer_rows(client):
    """ کل لیست مشتریان هلو (فقط خواندن)؛ None اگر خواندن ممکن نبود (mock/خطا) """
    data = client.get_json('Customer', timeout=max(client.config.timeout, 180))
    if not isinstance(data, dict) or 'Customer' not in data:
        return None
    return data['Customer']


@dataclass
class Report:
    counts: Counter = field(default_factory=Counter)
    entries: list = field(default_factory=list)       # برای CSV: dict(code, name, mobile, outcome, detail)

    def add(self, row, outcome, mobile='', detail=''):
        self.counts[outcome] += 1
        self.entries.append({'code': row.get('Code', ''), 'name': clean_name(row.get('Name')), 'mobile': mobile or row.get('Mobile', ''),
                             'outcome': outcome, 'detail': detail})


OUTCOME_LABELS = {
    'created': 'کاربر تازه ساخته شد',
    'linked': 'به کاربرِ موجودِ سایت وصل شد',
    'approved_existing': 'کاربر موجود (با پروفایل کامل) تأیید شد',
    'already': 'از قبل وصل بود',
    'conflict': 'تداخل (بررسی دستی)',
    'error': 'خطا',
    'would_create': 'ساخته می‌شود',
    'would_link': 'به کاربر موجود وصل می‌شود',
}


def _fill_holoo_fields(user, row):
    user.holoo_customer_code = user.holoo_customer_code or (str(row['Code']) if row.get('Code') else None)
    user.holoo_bed_sarfasl = user.holoo_bed_sarfasl or (str(row['BedSarfasl']) if row.get('BedSarfasl') else None)
    user.holoo_full_name = user.holoo_full_name or clean_name(row.get('Name'))[:255]
    user.imported_from_holoo = True


def apply_plan(plan, apply=True):
    """
    برنامه را روی دیتابیس سایت اجرا می‌کند (apply=False: فقط می‌گوید چه می‌شد). هر مشتری در تراکنش خودش است؛ خطای یکی
    بقیه را نمی‌اندازد.
    """
    report = Report()
    users_by_phone, users_by_erp = {}, {}
    for user in CustomUser.objects.all():
        users_by_phone[user.phone_number] = user
        if user.erp_code:
            users_by_erp.setdefault(user.erp_code, user)

    for mobile, row in plan.candidates:
        erp = row['ErpCode']
        user = users_by_phone.get(mobile)
        owner = users_by_erp.get(erp)
        try:
            if owner is not None and (user is None or owner.pk != user.pk):
                report.add(row, 'conflict', mobile, f'این کد هلو قبلاً به کاربر {owner.phone_number} وصل است.')
            elif user is None:
                if not apply:
                    report.add(row, 'would_create', mobile)
                    continue
                first_name, last_name = split_name(row.get('Name'))
                with transaction.atomic():
                    new_user = CustomUser.objects.create_user(
                        mobile, first_name=first_name or None, last_name=last_name or None, erp_code=erp,
                        status=UserStatus.PENDING_PROFILE, price_level=price_level_of(row),
                        holoo_customer_code=str(row['Code']) if row.get('Code') else None,
                        holoo_bed_sarfasl=str(row['BedSarfasl']) if row.get('BedSarfasl') else None,
                        imported_from_holoo=True, holoo_full_name=clean_name(row.get('Name'))[:255],
                    )
                users_by_phone[mobile] = new_user
                users_by_erp[erp] = new_user
                report.add(row, 'created', mobile)
            elif user.erp_code and user.erp_code != erp:
                report.add(row, 'conflict', mobile, 'این موبایل در سایت به مشتری دیگری از هلو وصل است.')
            elif user.erp_code == erp:
                if apply and not user.imported_from_holoo:
                    with transaction.atomic():
                        _fill_holoo_fields(user, row)
                        user.save(update_fields=['holoo_customer_code', 'holoo_bed_sarfasl', 'holoo_full_name', 'imported_from_holoo'])
                report.add(row, 'already', mobile)
            else:
                if not apply:
                    report.add(row, 'would_link', mobile)
                    continue
                with transaction.atomic():
                    user.erp_code = erp
                    _fill_holoo_fields(user, row)
                    fields = ['erp_code', 'holoo_customer_code', 'holoo_bed_sarfasl', 'holoo_full_name', 'imported_from_holoo']
                    if user.approval_status == ApprovalStatus.PENDING:
                        user.price_level = price_level_of(row)
                        fields.append('price_level')
                    user.save(update_fields=fields)
                    _, approved = (user.approve_if_trusted_import() or (None, False))
                users_by_erp[erp] = user
                report.add(row, 'approved_existing' if approved else 'linked', mobile)
        except Exception as error:  # یک مشتری بد نباید کل ورود را بیندازد
            report.add(row, 'error', mobile, f'{type(error).__name__}: {error}')
    return report


def csv_rows(plan, report):
    """ سطرهای گزارش CSV: نتیجه‌ی ورود + ردیف‌های ردشده با دلیل """
    rows = [(e['code'], e['name'], e['mobile'], OUTCOME_LABELS.get(e['outcome'], e['outcome']), e['detail']) for e in report.entries]
    rows += [(row.get('Code', ''), clean_name(row.get('Name')), row.get('Mobile', ''), REASON_LABELS[reason], detail)
             for row, reason, detail in plan.skipped if reason not in ('not_purchaser', 'inactive', 'no_mobile', 'no_erp')]
    return rows
