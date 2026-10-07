"""
ثبت اطلاعات چک برای سفارش‌های چکی (فاز B): اعتبارسنجی فرم، اعتبارسنجی و پاک‌سازی تصاویر، ساخت اتمیک چک + تصاویر، و پاسخ امن دانلود.

قواعد:
  - فقط سفارش چکیِ خودِ کاربر، تا وقتی لغو/ردشده/تأییدشده نیست؛ حداکثر MAX_CHEQUES_PER_ORDER چک برای هر سفارش.
  - شناسه‌ی صیادی ۱۶ رقم (ارقام فارسی/عربی هم پذیرفته و به لاتین تبدیل می‌شود) و الزامی؛ حداقل ۱ و حداکثر ۵ تصویر برای هر چک.
  - تصویر: فقط JPG/PNG/WebP، نوع از magic bytes، دوباره‌کدگذاری با Pillow (حذف EXIF)، نام تصادفی uuid در MEDIA_ROOT/cheque_images/،
    بدون نشانی عمومی (services/safe_images.py، orders/storage.py).
  - اگر وسط ساخت خطا بیاید، چک و فایل‌های نوشته‌شده هیچ ردی نمی‌گذارند.
"""
import logging
import re
from dataclasses import dataclass
from datetime import date

import jdatetime
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.db import transaction
from django.http import FileResponse
from django.utils import timezone

from services.safe_images import IMAGE_TYPES, UnsafeUpload, clean_image, sniff_image
from services.text import to_latin_digits

from .models import ChequeImage, ChequePayment, Order

logger = logging.getLogger(__name__)

MAX_IMAGES = 5                    # تصویر برای هر چک (رو، پشت یا مستندات)
MAX_IMAGE_MB = 5
MAX_CHEQUES_PER_ORDER = 10
RATE_LIMIT = 20                   # ثبت چک برای هر کاربر در RATE_WINDOW ثانیه (دیکد تصویر گران است)
RATE_WINDOW = 600
MAX_AMOUNT = 10 ** 12
ALLOWED_ACCEPT = 'image/jpeg,image/png,image/webp'

_CONTROL = re.compile(r'[\x00-\x1f\x7f]')


class ChequeError(Exception):
    """ خطای اعتبارسنجی؛ errors = {نام فیلد: پیام فارسی}، فیلد '__all__' برای خطای کلی """

    def __init__(self, errors, status=400):
        super().__init__(str(errors))
        self.errors, self.status = errors, status


@dataclass
class PreparedImage:
    data: bytes
    content_type: str
    ext: str
    name: str
    width: int
    height: int


# ------------------------------------------------------------------ اعتبارسنجی فیلدها

def normalize_sayadi(raw):
    value = to_latin_digits(str(raw or '')).replace(' ', '').replace('-', '').replace('‌', '')
    if not value:
        raise ChequeError({'sayadi_id': 'شناسه‌ی صیادی را وارد کنید.'})
    if not value.isascii() or not value.isdigit() or len(value) != 16:
        raise ChequeError({'sayadi_id': 'شناسه‌ی صیادی باید دقیقاً ۱۶ رقم باشد.'})
    return value


def parse_amount(raw):
    text = to_latin_digits(str(raw or '')).replace(',', '').replace('٬', '').replace(' ', '')
    if not text:
        return None
    if not text.isascii() or not text.isdigit() or int(text) <= 0 or int(text) > MAX_AMOUNT:
        raise ChequeError({'amount': 'مبلغ چک را به تومان و فقط با عدد وارد کنید.'})
    return int(text)


def parse_due_date(raw):
    """ تاریخ شمسی «۱۴۰۵/۰۸/۱۵» (انتخابگر شمسی فرم)؛ تاریخ میلادی «2026-11-06» هم پذیرفته می‌شود. خالی ← None """
    text = to_latin_digits(str(raw or '')).strip().replace('-', '/')
    if not text:
        return None
    try:
        year = int(text.split('/')[0])
        if year >= 1700:
            parts = [int(p) for p in text.split('/')]
            return date(*parts)
        return jdatetime.datetime.strptime(text, '%Y/%m/%d').togregorian().date()
    except (ValueError, TypeError, IndexError):
        raise ChequeError({'due_date': 'قالب تاریخ سررسید نامعتبر است (مثلاً ۱۴۰۵/۰۸/۱۵).'})


def clean_text(raw, limit, field, label):
    text = ' '.join(_CONTROL.sub(' ', str(raw or '')).split())
    if len(text) > limit:
        raise ChequeError({field: f'{label} حداکثر {limit} نویسه است.'})
    return text


def prepare_images(files):
    """ فایل‌های آپلودشده ← فهرست PreparedImage تمیز (۱ تا MAX_IMAGES)، یا ChequeError """
    files = [f for f in files if f is not None]
    if not files:
        raise ChequeError({'images': 'حداقل یک تصویر از چک لازم است.'})
    if len(files) > MAX_IMAGES:
        raise ChequeError({'images': f'حداکثر {MAX_IMAGES} تصویر برای هر چک مجاز است.'})
    limit = MAX_IMAGE_MB * 1024 * 1024
    prepared = []
    for upload in files:
        if upload.size > limit:
            raise ChequeError({'images': f'حجم هر تصویر حداکثر {MAX_IMAGE_MB} مگابایت است.'})
        data = upload.read(limit + 1)
        if len(data) > limit:
            raise ChequeError({'images': f'حجم هر تصویر حداکثر {MAX_IMAGE_MB} مگابایت است.'})
        detected = sniff_image(data[:16])
        if detected is None:
            raise ChequeError({'images': 'فقط تصویر JPG، PNG یا WebP مجاز است.'})
        try:
            cleaned, width, height = clean_image(data, detected)
        except UnsafeUpload as error:
            raise ChequeError({'images': error.message})
        content_type, ext = IMAGE_TYPES[detected]
        name = re.sub(r'[\x00-\x1f\x7f<>:"|?*]', '', str(upload.name or '').replace('\\', '/').rsplit('/', 1)[-1]).strip()[:80]
        prepared.append(PreparedImage(cleaned, content_type, ext, name or 'cheque', width, height))
    return prepared


# ------------------------------------------------------------------ قواعد ثبت

def submission_blocker(order):
    """ دلیل غیرممکن بودن ثبت چک تازه برای این سفارش (متن فارسی) یا None """
    if not order.is_cheque:
        return 'این سفارش با روش چکی ثبت نشده است.'
    if order.status in ('canceled', 'rejected_stock'):
        return 'این سفارش لغو شده است.'
    if order.approved_at:
        return 'این سفارش تأیید شده است و ثبت چک تازه ممکن نیست.'
    if order.cheques.count() >= MAX_CHEQUES_PER_ORDER:
        return f'حداکثر {MAX_CHEQUES_PER_ORDER} چک برای هر سفارش مجاز است.'
    return None


def _rate_limited(user):
    key = f'cheque:rl:{user.pk}'
    try:
        if cache.add(key, 1, RATE_WINDOW):
            return False
        return cache.incr(key) > RATE_LIMIT
    except Exception:  # noqa: BLE001 - قطع کش ثبت چک را متوقف نمی‌کند (سقف تعداد چک هر سفارش همچنان برقرار است)
        return False


def create_cheque(order, user, data, files):
    """
    چک + تصاویرش را اتمیک می‌سازد. data: dict خام فرم (sayadi_id، amount، due_date، bank_name، holder_name).
    همه‌ی خطاهای فیلدها یک‌جا (ChequeError.errors) برمی‌گردند. ← ChequePayment
    """
    blocker = submission_blocker(order)
    if blocker:
        raise ChequeError({'__all__': blocker}, status=409)
    if _rate_limited(user):
        raise ChequeError({'__all__': 'تعداد تلاش‌های شما زیاد بود؛ چند دقیقه بعد دوباره تلاش کنید.'}, status=429)

    errors, values = {}, {}
    for field, parser in (
        ('sayadi_id', normalize_sayadi), ('amount', parse_amount), ('due_date', parse_due_date),
        ('bank_name', lambda raw: clean_text(raw, 60, 'bank_name', 'نام بانک')),
        ('holder_name', lambda raw: clean_text(raw, 100, 'holder_name', 'نام صاحب حساب')),
    ):
        try:
            values[field] = parser(data.get(field))
        except ChequeError as error:
            errors.update(error.errors)
    prepared = []
    try:
        prepared = prepare_images(files)                      # بعد از محدودیت نرخ: decode تصویر گران است
    except ChequeError as error:
        errors.update(error.errors)
    if errors:
        raise ChequeError(errors)

    sayadi = values['sayadi_id']
    duplicate = (ChequePayment.objects.filter(sayadi_id=sayadi)
                 .exclude(status=ChequePayment.STATUS_REJECTED).exclude(order__status__in=('canceled', 'rejected_stock')))
    if duplicate.exists():
        raise ChequeError({'sayadi_id': 'این شناسه‌ی صیادی قبلاً در سامانه ثبت شده است؛ اگر اشتباه نیست با پشتیبانی تماس بگیرید.'})

    saved = []
    try:
        with transaction.atomic():
            locked = Order.objects.select_for_update().get(pk=order.pk)      # دو ثبت هم‌زمان از سقف تعداد نمی‌گذرند
            blocker = submission_blocker(locked)
            if blocker:
                raise ChequeError({'__all__': blocker}, status=409)
            cheque = ChequePayment.objects.create(
                order=locked, sayadi_id=sayadi, amount=values['amount'], due_date=values['due_date'],
                bank_name=values['bank_name'], holder_name=values['holder_name'])
            for item in prepared:
                image = ChequeImage(cheque=cheque, ext=item.ext, content_type=item.content_type, original_name=item.name,
                                    size=len(item.data), width=item.width, height=item.height)
                image.file.save(f'{image.public_id}.{item.ext}', ContentFile(item.data), save=False)
                saved.append(image.file)
                image.save()
    except BaseException:
        for field in saved:
            try:
                field.storage.delete(field.name)
            except Exception:  # noqa: BLE001 - پاک‌سازی بهترین‌تلاش است
                logger.warning('حذف تصویر نیمه‌کاره‌ی چک ناموفق بود: %s', getattr(field, 'name', ''))
        raise
    return cheque


# ------------------------------------------------------------------ دانلود امن

def image_response(image):
    """ پاسخ دانلود: تصویر inline (دوباره‌کدشده)، با nosniff و CSP sandbox؛ فقط از ویوهای دارای کنترل دسترسی صدا زده شود """
    try:
        handle = image.file.open('rb')
    except (FileNotFoundError, ValueError, OSError):
        return None
    response = FileResponse(handle, content_type=image.content_type)
    response['Content-Disposition'] = f'inline; filename="{image.public_id}.{image.ext}"'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Content-Security-Policy'] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox"
    response['Cache-Control'] = 'private, max-age=3600'
    return response
