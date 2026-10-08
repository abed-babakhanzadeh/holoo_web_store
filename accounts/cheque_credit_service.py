"""
سرویس درخواست خرید چکی / اعتباری (فاز F1): ثبت، انصراف، تأیید، رد و پاک‌سازی مدارک. ویو، ادمین و تسک‌ها فقط همین‌جا را صدا می‌زنند؛
منطق و قفل‌ها یک‌جاست (هم‌الگوی orders/cheques.py).

قواعد:
  - فقط کاربرِ تأییدشده‌ای که چکیِ مستقیم ندارد و گزینه‌ی «درخواست خرید چکی» برایش نمایش داده می‌شود
    (orders.payment_options.request_option: مشتری نقدی، یا ویژه با سیاست request_check) می‌تواند درخواست بدهد؛ نه کسی که مجوز فردی دارد.
  - نام، نام‌خانوادگی و کد ملی از پروفایل برداشته می‌شود (ورودی کاربر نیست)؛ کد ملی ده‌رقمی و (با تنظیم ادمین، پیش‌فرض روشن) با رقم کنترل.
  - هر کاربر حداکثر یک درخواست pending؛ قفل ردیف کاربر + ایندکس یکتای دیتابیس. درخواست مجدد بعد از رد/انصراف آزاد است (فقط محدودیت نرخ).
  - مدرک: فقط JPG/PNG/WebP (services/safe_images.py)، حداکثر ۵ فایل و هر کدام ۵ مگابایت؛ دست‌کم یک «تصویر دسته‌چک».
  - تأیید: اتمیک و فقط از pending؛ can_purchase_with_check خودکار روشن می‌شود (با update، بدون ذخیره‌ی مدل کاربر و سیگنال‌های آن؛
    سطح قیمت و هلو دست نمی‌خورد). رد: علت الزامی. تأیید/رد دوباره و روی درخواست انصراف‌داده‌شده ممکن نیست.
  - سیگنال‌ها همیشه پس از commit.
"""
import logging
import re
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.http import FileResponse
from django.utils import timezone

from products.models import SiteSettings, is_valid_iranian_national_code
from services.safe_images import IMAGE_TYPES, UnsafeUpload, clean_image, sniff_image
from services.text import to_latin_digits

from .address import _lock_user_rows
from .cheque_credit import ChequeCreditDocument, ChequeCreditRequest
from .models import CustomUser
from .signals import cheque_credit_approved, cheque_credit_rejected, cheque_credit_requested

logger = logging.getLogger(__name__)

MAX_DOCS = 5
MAX_DOC_MB = 5
RATE_LIMIT = 5                    # ثبت درخواست برای هر کاربر در RATE_WINDOW ثانیه (دیکد تصویر گران است)
RATE_WINDOW = 3600
REASON_MAX = 300
NOTE_MAX = 500
DESCRIPTION_MAX = 1000
MAX_AMOUNT = 10 ** 12
PURGE_BATCH = 200

_CONTROL = re.compile(r'[\x00-\x1f\x7f]')
_IBAN_DIGITS = re.compile(r'^\d{24}$')
_NATIONAL_CODE = re.compile(r'^\d{10}$')


class ChequeCreditError(Exception):
    """ خطای اعتبارسنجی/وضعیت؛ errors = {نام فیلد: پیام فارسی}، فیلد '__all__' برای خطای کلی. status مثل کد HTTP """

    def __init__(self, errors, status=400):
        super().__init__(str(errors))
        self.errors, self.status = errors, status

    @property
    def message(self):
        return ' '.join(str(v) for v in self.errors.values())


# ------------------------------------------------------------------ اعتبارسنجی

def clean_text(raw, limit, field, label, *, required=False):
    text = ' '.join(_CONTROL.sub(' ', str(raw or '')).split())
    if required and not text:
        raise ChequeCreditError({field: f'{label} را وارد کنید.'})
    if len(text) > limit:
        raise ChequeCreditError({field: f'{label} حداکثر {limit} نویسه است.'})
    return text


def normalize_iban(raw):
    """ «IR» + ۲۴ رقم (ارقام فارسی/عربی، فاصله و خط‌تیره پذیرفته)؛ کنترل mod-97 هم انجام می‌شود """
    value = to_latin_digits(str(raw or '')).upper().replace(' ', '').replace('-', '').replace('‌', '')
    if not value:
        raise ChequeCreditError({'iban': 'شماره شبا را وارد کنید.'})
    digits = value[2:] if value.startswith('IR') else value
    if not _IBAN_DIGITS.match(digits):
        raise ChequeCreditError({'iban': 'شماره شبا باید ۲۴ رقم باشد (با یا بدون پیشوند IR).'})
    # mod-97 استاندارد: ۴ نویسه‌ی اول (IR + رقم کنترل) به انتها می‌رود و حروف عدد می‌شوند (I=18، R=27)
    if int(digits[2:] + '1827' + digits[:2]) % 97 != 1:
        raise ChequeCreditError({'iban': 'شماره شبا معتبر نیست؛ ارقام را دوباره بررسی کنید.'})
    return f'IR{digits}'


def parse_amount(raw, field, label, *, required=False):
    text = to_latin_digits(str(raw or '')).replace(',', '').replace('٬', '').replace(' ', '')
    if not text:
        if required:
            raise ChequeCreditError({field: f'{label} را وارد کنید.'})
        return None
    if not text.isascii() or not text.isdigit() or int(text) <= 0 or int(text) > MAX_AMOUNT:
        raise ChequeCreditError({field: f'{label} را به تومان و فقط با عدد (بیشتر از صفر) وارد کنید.'})
    return Decimal(int(text))


def _parse_fields(data):
    """ فیلدهای متنی/عددی فرم ← (values، errors)؛ همه‌ی خطاها یک‌جا """
    errors, values = {}, {}
    parsers = (
        ('business_name', lambda v: clean_text(v, 255, 'business_name', 'نام فروشگاه/شرکت', required=True)),
        ('bank_name', lambda v: clean_text(v, 60, 'bank_name', 'نام بانک', required=True)),
        ('account_holder', lambda v: clean_text(v, 100, 'account_holder', 'نام صاحب حساب', required=True)),
        ('iban', normalize_iban),
        ('requested_limit', lambda v: parse_amount(v, 'requested_limit', 'سقف اعتبار درخواستی', required=True)),
        ('monthly_turnover', lambda v: parse_amount(v, 'monthly_turnover', 'میانگین گردش ماهانه')),
        ('description', lambda v: clean_text(v, DESCRIPTION_MAX, 'description', 'توضیحات')),
    )
    for field, parser in parsers:
        try:
            values[field] = parser(data.get(field))
        except ChequeCreditError as error:
            errors.update(error.errors)
    return values, errors


def prepare_documents(files):
    """
    files: فهرست (kind، UploadedFile). ← فهرست مدرک تمیز (kind، بایت‌ها، content_type، پسوند، نام، عرض، ارتفاع) یا ChequeCreditError.
    حداقل یک تصویر دسته‌چک و حداکثر MAX_DOCS فایل؛ نوع از magic bytes و تصویر دوباره‌کدگذاری می‌شود.
    """
    files = [(kind, f) for kind, f in files if f is not None]
    kinds = dict(ChequeCreditDocument.KIND_CHOICES)
    if any(kind not in kinds for kind, _ in files):
        raise ChequeCreditError({'documents': 'نوع مدرک نامعتبر است.'})
    if not any(kind == ChequeCreditDocument.KIND_CHEQUE_BOOK for kind, _ in files):
        raise ChequeCreditError({'documents': 'تصویر دسته‌چک الزامی است.'})
    if len(files) > MAX_DOCS:
        raise ChequeCreditError({'documents': f'حداکثر {MAX_DOCS} تصویر مجاز است.'})
    limit = MAX_DOC_MB * 1024 * 1024
    prepared = []
    for kind, upload in files:
        if upload.size > limit:
            raise ChequeCreditError({'documents': f'حجم هر تصویر حداکثر {MAX_DOC_MB} مگابایت است.'})
        data = upload.read(limit + 1)
        if len(data) > limit:
            raise ChequeCreditError({'documents': f'حجم هر تصویر حداکثر {MAX_DOC_MB} مگابایت است.'})
        detected = sniff_image(data[:16])
        if detected is None:
            raise ChequeCreditError({'documents': 'فقط تصویر JPG، PNG یا WebP مجاز است (PDF پذیرفته نمی‌شود).'})
        try:
            cleaned, width, height = clean_image(data, detected)
        except UnsafeUpload as error:
            raise ChequeCreditError({'documents': error.message})
        content_type, ext = IMAGE_TYPES[detected]
        name = re.sub(r'[\x00-\x1f\x7f<>:"|?*]', '', str(upload.name or '').replace('\\', '/').rsplit('/', 1)[-1]).strip()[:80]
        prepared.append((kind, cleaned, content_type, ext, name or 'document', width, height))
    return prepared


# ------------------------------------------------------------------ شرایط ثبت

def eligibility_blocker(user):
    """ دلیل غیرمجاز بودن ثبت درخواست برای این کاربر (متن فارسی) یا None. وضعیت pending جداگانه در submit_request سنجیده می‌شود. """
    if user is None or not getattr(user, 'is_authenticated', False):
        return 'برای ثبت درخواست ابتدا وارد حساب کاربری شوید.'
    if not user.can_order():
        return 'حساب شما هنوز تأیید نشده است؛ پس از تأیید حساب می‌توانید درخواست خرید چکی ثبت کنید.'
    if user.can_purchase_with_check:
        return 'مجوز خرید چکی برای حساب شما فعال است و نیازی به درخواست نیست.'
    from orders import payment_options                          # وارد کردن دیرهنگام: accounts به orders وابسته نشود
    if payment_options.request_option(user) is None:
        return 'ثبت درخواست خرید چکی برای حساب شما فعال نیست.'
    if not (user.first_name and user.last_name and _NATIONAL_CODE.match(str(user.national_code or ''))):
        return 'ابتدا نام، نام خانوادگی و کد ملی را در پروفایل خود کامل کنید.'
    return None


def national_code_problem(user):
    """
    پیام خطای کد ملی پروفایل یا None. همیشه قالب ده‌رقمی؛ و اگر SiteSettings.strict_national_code_validation روشن باشد (پیش‌فرض)،
    رقم کنترلی رسمی هم سنجیده می‌شود (is_valid_iranian_national_code).
    """
    code = str(getattr(user, 'national_code', '') or '')
    if not _NATIONAL_CODE.match(code):
        return 'کد ملی ثبت‌شده در پروفایل باید ده رقم باشد؛ آن را اصلاح کنید.'
    if SiteSettings.cached().strict_national_code_validation and not is_valid_iranian_national_code(code):
        return 'کد ملی ثبت‌شده در پروفایل معتبر نیست (رقم کنترلی نمی‌خواند)؛ آن را اصلاح کنید.'
    return None


def pending_request(user):
    return ChequeCreditRequest.objects.filter(user=user, status=ChequeCreditRequest.STATUS_PENDING).first()


def _rate_limited(user):
    key = f'cheque_credit:rl:{user.pk}'
    try:
        if cache.add(key, 1, RATE_WINDOW):
            return False
        return cache.incr(key) > RATE_LIMIT
    except Exception:  # noqa: BLE001 - قطع کش ثبت را متوقف نمی‌کند (قید pending یکتا همچنان برقرار است)
        return False


# ------------------------------------------------------------------ ثبت

def submit_request(user, data, files):
    """
    درخواست + مدارکش را اتمیک می‌سازد. data: dict خام فرم (business_name، bank_name، account_holder، iban، requested_limit،
    monthly_turnover، description)؛ files: فهرست (kind، UploadedFile). همه‌ی خطاهای فیلدها یک‌جا (ChequeCreditError.errors).
    ← ChequeCreditRequest
    """
    blocker = eligibility_blocker(user)
    if blocker:
        raise ChequeCreditError({'__all__': blocker}, status=409)
    problem = national_code_problem(user)
    if problem:
        raise ChequeCreditError({'national_code': problem})
    if pending_request(user) is not None:
        raise ChequeCreditError({'__all__': 'یک درخواست شما هم‌اکنون در انتظار بررسی است.'}, status=409)
    if _rate_limited(user):
        raise ChequeCreditError({'__all__': 'تعداد تلاش‌های شما زیاد بود؛ چند دقیقه بعد دوباره تلاش کنید.'}, status=429)

    values, errors = _parse_fields(data)
    prepared = []
    try:
        prepared = prepare_documents(files)                       # بعد از محدودیت نرخ: decode تصویر گران است
    except ChequeCreditError as error:
        errors.update(error.errors)
    if errors:
        raise ChequeCreditError(errors)

    saved = []
    try:
        with transaction.atomic():
            _lock_user_rows([user.pk])                            # دو ثبت هم‌زمان یک کاربر پشت هم صف می‌شوند
            fresh = CustomUser.objects.get(pk=user.pk)
            blocker = eligibility_blocker(fresh)
            if blocker:
                raise ChequeCreditError({'__all__': blocker}, status=409)
            problem = national_code_problem(fresh)
            if problem:
                raise ChequeCreditError({'national_code': problem})
            if pending_request(fresh) is not None:
                raise ChequeCreditError({'__all__': 'یک درخواست شما هم‌اکنون در انتظار بررسی است.'}, status=409)
            request = ChequeCreditRequest.objects.create(
                user=fresh, first_name=fresh.first_name, last_name=fresh.last_name, national_code=str(fresh.national_code),
                **values)
            for kind, cleaned, content_type, ext, name, width, height in prepared:
                doc = ChequeCreditDocument(request=request, kind=kind, ext=ext, content_type=content_type, original_name=name,
                                           size=len(cleaned), width=width, height=height)
                doc.file.save(f'{doc.public_id}.{ext}', ContentFile(cleaned), save=False)
                saved.append(doc.file)
                doc.save()
            transaction.on_commit(lambda: cheque_credit_requested.send_robust(sender=ChequeCreditRequest, request=request))
    except IntegrityError:
        _cleanup_files(saved)
        raise ChequeCreditError({'__all__': 'یک درخواست شما هم‌اکنون در انتظار بررسی است.'}, status=409)
    except BaseException:
        _cleanup_files(saved)
        raise
    return request


def _cleanup_files(fields):
    for field in fields:
        try:
            field.storage.delete(field.name)
        except Exception:  # noqa: BLE001 - پاک‌سازی بهترین‌تلاش است
            logger.warning('حذف مدرک نیمه‌کاره‌ی درخواست خرید چکی ناموفق بود: %s', getattr(field, 'name', ''))


# ------------------------------------------------------------------ انصراف / تأیید / رد

def _lock_pending(request, expected_statuses=(ChequeCreditRequest.STATUS_PENDING,)):
    """ داخل atomic: ردیف درخواست را قفل و وضعیت را می‌سنجد (تأیید/رد/انصراف هم‌زمان فقط یکی برنده می‌شود) """
    locked = ChequeCreditRequest.objects.select_for_update().get(pk=request.pk)
    if locked.status not in expected_statuses:
        raise ChequeCreditError({'__all__': f'این درخواست قبلاً «{locked.get_status_display()}» شده است و تغییر نمی‌کند.'}, status=409)
    return locked


def cancel_request(request, user):
    """ انصراف مشتری از درخواستِ در انتظار خودش. درخواست دیگران ← ۴۰۴ (وجود آن لو نمی‌رود). """
    if request.user_id != getattr(user, 'pk', None):
        raise ChequeCreditError({'__all__': 'درخواست یافت نشد.'}, status=404)
    with transaction.atomic():
        _lock_user_rows([request.user_id])
        locked = _lock_pending(request)
        locked.status = ChequeCreditRequest.STATUS_CANCELED
        locked.decided_at = timezone.now()
        locked.save(update_fields=['status', 'decided_at', 'updated_at'])
    return locked


def _clean_note(note):
    return clean_text(note, NOTE_MAX, 'admin_note', 'یادداشت')


def approve_request(request, by, approved_limit=None, admin_note=''):
    """
    تأیید مدیر: اتمیک و فقط از pending. ردیف کاربر و درخواست قفل می‌شود، وضعیت و تأییدکننده ثبت می‌شود و
    CustomUser.can_purchase_with_check روشن می‌شود (update مستقیم: سطح قیمت، هلو و سیگنال‌های تأیید حساب لمس نمی‌شود).
    approved_limit فقط اطلاعاتی است (اختیاری). سیگنال cheque_credit_approved پس از commit. ← درخواست به‌روز
    """
    limit = None
    if approved_limit not in (None, ''):
        limit = parse_amount(approved_limit, 'approved_limit', 'سقف اعتبار تأییدشده')
    note = _clean_note(admin_note)
    with transaction.atomic():
        _lock_user_rows([request.user_id])
        locked = _lock_pending(request)
        now = timezone.now()
        locked.status = ChequeCreditRequest.STATUS_APPROVED
        locked.reviewed_by = by if getattr(by, 'pk', None) else None
        locked.reviewed_at = locked.decided_at = now
        locked.approved_limit = limit
        locked.rejection_reason = ''
        if note:
            locked.admin_note = note
        locked.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'decided_at', 'approved_limit', 'rejection_reason',
                                   'admin_note', 'updated_at'])
        CustomUser.objects.filter(pk=locked.user_id).update(can_purchase_with_check=True)
        transaction.on_commit(lambda: cheque_credit_approved.send_robust(sender=ChequeCreditRequest, request=locked))
    return locked


def reject_request(request, by, reason, admin_note=''):
    """ رد مدیر: علت الزامی (≤ REASON_MAX)، فقط از pending. مجوز کاربر دست نمی‌خورد. سیگنال پس از commit. """
    reason = ' '.join(_CONTROL.sub(' ', str(reason or '')).split())
    if not reason:
        raise ChequeCreditError({'rejection_reason': 'برای رد درخواست، علت رد را بنویسید (به مشتری نمایش داده می‌شود).'})
    if len(reason) > REASON_MAX:
        raise ChequeCreditError({'rejection_reason': f'علت رد حداکثر {REASON_MAX} نویسه است.'})
    note = _clean_note(admin_note)
    with transaction.atomic():
        _lock_user_rows([request.user_id])
        locked = _lock_pending(request)
        now = timezone.now()
        locked.status = ChequeCreditRequest.STATUS_REJECTED
        locked.reviewed_by = by if getattr(by, 'pk', None) else None
        locked.reviewed_at = locked.decided_at = now
        locked.rejection_reason = reason
        if note:
            locked.admin_note = note
        locked.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'decided_at', 'rejection_reason', 'admin_note',
                                   'updated_at'])
        transaction.on_commit(lambda: cheque_credit_rejected.send_robust(sender=ChequeCreditRequest, request=locked))
    return locked


# ------------------------------------------------------------------ پاک‌سازی مدارک

def retention_days():
    return int(SiteSettings.cached().cheque_credit_docs_retention_days or 0)


def purge_expired_documents(now=None, limit=PURGE_BATCH):
    """
    تصاویر مدارکِ درخواست‌های تعیین‌تکلیف‌شده (تأیید/رد/انصراف) که از decided_at بیش از retention_days روز گذشته پاک می‌شود؛ اطلاعات
    متنی درخواست می‌ماند و documents_purged_at ثبت می‌شود. retention_days=0 ← هیچ کاری نمی‌کند. درخواست pending هرگز. ← تعداد درخواست‌های
    پاک‌سازی‌شده. (زمان‌بندی دوره‌ای در فاز بعد وصل می‌شود.)
    """
    days = retention_days()
    if days <= 0:
        return 0
    now = now or timezone.now()
    cutoff = now - timedelta(days=days)
    ids = list(ChequeCreditRequest.objects.filter(status__in=ChequeCreditRequest.FINAL_STATUSES, decided_at__isnull=False,
                                                  decided_at__lte=cutoff, documents_purged_at__isnull=True)
               .order_by('decided_at').values_list('pk', flat=True)[:limit])
    purged = 0
    for pk in ids:
        try:
            with transaction.atomic():
                locked = ChequeCreditRequest.objects.select_for_update().get(pk=pk)
                if locked.documents_purged_at or locked.status == ChequeCreditRequest.STATUS_PENDING:
                    continue
                for doc in list(locked.documents.all()):
                    doc.delete()                                  # فایل با django_cleanup بعد از commit پاک می‌شود
                locked.documents_purged_at = now
                locked.save(update_fields=['documents_purged_at', 'updated_at'])
            purged += 1
        except Exception:  # noqa: BLE001 - خطای یک درخواست بقیه را متوقف نمی‌کند
            logger.exception('پاک‌سازی مدارک درخواست خرید چکی %s ناموفق بود.', pk)
    return purged


# ------------------------------------------------------------------ دانلود امن

def document_response(document):
    """ پاسخ نمایش مدرک: inline (تصویر دوباره‌کدشده)، با nosniff و CSP sandbox؛ فقط از ویوهای دارای کنترل دسترسی صدا زده شود. فایل نبود ← None """
    try:
        handle = document.file.open('rb')
    except (FileNotFoundError, ValueError, OSError):
        return None
    response = FileResponse(handle, content_type=document.content_type)
    response['Content-Disposition'] = f'inline; filename="{document.public_id}.{document.ext}"'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Content-Security-Policy'] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox"
    response['Cache-Control'] = 'private, no-store'
    return response
