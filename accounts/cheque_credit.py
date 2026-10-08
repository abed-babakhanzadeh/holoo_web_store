"""
درخواست خرید چکی / اعتباری (فاز F1).

مشتریِ نقدی (یا ویژه با سیاست «درخواست») که چکی مستقیم ندارد، اطلاعات و مدارک اعتباری‌اش را ثبت می‌کند؛ مدیر بررسی می‌کند. با
«تأیید»، CustomUser.can_purchase_with_check خودکار روشن می‌شود (سرویس: accounts/cheque_credit_service.py). «سقف اعتبارِ تأییدشده»
فعلاً فقط اطلاعاتی و مدیریتی است و در تسویه‌حساب اعمال نمی‌شود.

وضعیت‌ها: pending ← approved | rejected (با علت الزامی) | canceled (انصراف مشتری، فقط از pending). تأیید/رد/انصراف نهایی‌اند.

قید همزمانی: هر کاربر حداکثر یک درخواست pending دارد (ایندکس یکتای فیلترشده روی user؛ هم‌الگوی آدرس پیش‌فرض)؛ علاوه بر قفل ردیف کاربر
در سرویس، دیتابیس هم درخواست pending دوم را رد می‌کند.

مدارک: فقط تصویر (JPG/PNG/WebP، خروجی services/safe_images.py)، ذخیره‌ی خصوصی بدون نشانی عمومی، نام uuid. پس از تعیین‌تکلیف نهایی و
گذشت SiteSettings.cheque_credit_docs_retention_days روز، تصاویر پاک می‌شوند (۰ = پاک‌سازی خاموش).
"""
import uuid

from django.db import models
from django.db.models import Q
from django.utils import timezone

from .credit_storage import cheque_credit_doc_storage


class ChequeCreditRequest(models.Model):
    STATUS_PENDING = 'pending'
    STATUS_APPROVED = 'approved'
    STATUS_REJECTED = 'rejected'
    STATUS_CANCELED = 'canceled'                     # املای یک‌L مثل وضعیت سفارش در این پروژه
    STATUS_CHOICES = (
        (STATUS_PENDING, 'در انتظار بررسی'),
        (STATUS_APPROVED, 'تأییدشده'),
        (STATUS_REJECTED, 'ردشده'),
        (STATUS_CANCELED, 'انصراف مشتری'),
    )
    FINAL_STATUSES = (STATUS_APPROVED, STATUS_REJECTED, STATUS_CANCELED)

    user = models.ForeignKey('accounts.CustomUser', on_delete=models.PROTECT, related_name='cheque_credit_requests',
                             verbose_name='کاربر')
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, verbose_name='شناسه‌ی عمومی')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True, verbose_name='وضعیت')

    # اسنپ‌شات هویتی از پروفایل در لحظه‌ی ثبت (ویرایش بعدیِ پروفایل سابقه را عوض نمی‌کند)
    first_name = models.CharField(max_length=50, verbose_name='نام')
    last_name = models.CharField(max_length=50, verbose_name='نام خانوادگی')
    national_code = models.CharField(max_length=10, verbose_name='کد ملی')

    # اطلاعات ارزیابی اعتبار
    business_name = models.CharField(max_length=255, verbose_name='نام فروشگاه/شرکت')
    bank_name = models.CharField(max_length=60, verbose_name='نام بانک')
    account_holder = models.CharField(max_length=100, verbose_name='نام صاحب حساب')
    iban = models.CharField(max_length=26, verbose_name='شماره شبا')
    requested_limit = models.DecimalField(max_digits=15, decimal_places=0, verbose_name='سقف اعتبار درخواستی (تومان)')
    monthly_turnover = models.DecimalField(max_digits=15, decimal_places=0, null=True, blank=True,
                                           verbose_name='میانگین گردش/خرید ماهانه (تومان)')
    description = models.CharField(max_length=1000, blank=True, default='', verbose_name='توضیحات مشتری')

    # بررسی مدیر
    reviewed_by = models.ForeignKey('accounts.CustomUser', null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
                                    verbose_name='بررسی‌کننده')
    reviewed_at = models.DateTimeField(null=True, blank=True, verbose_name='زمان بررسی')
    rejection_reason = models.CharField(max_length=300, blank=True, default='', verbose_name='علت رد',
                                        help_text='برای رد الزامی است و به مشتری نمایش داده می‌شود.')
    approved_limit = models.DecimalField(max_digits=15, decimal_places=0, null=True, blank=True,
                                         verbose_name='سقف اعتبار تأییدشده (تومان)',
                                         help_text='فعلاً فقط اطلاعاتی است و در تسویه‌حساب اعمال نمی‌شود.')
    admin_note = models.CharField(max_length=500, blank=True, default='', verbose_name='یادداشت داخلی مدیر')
    # زمان تعیین‌تکلیف نهایی (تأیید/رد/انصراف)؛ مبدأ مهلت نگهداری مدارک
    decided_at = models.DateTimeField(null=True, blank=True, db_index=True, verbose_name='زمان تعیین‌تکلیف')
    documents_purged_at = models.DateTimeField(null=True, blank=True, verbose_name='زمان پاک‌سازی مدارک')

    created_at = models.DateTimeField(auto_now_add=True, verbose_name='زمان ثبت')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='آخرین بروزرسانی')

    class Meta:
        verbose_name = 'درخواست خرید چکی'
        verbose_name_plural = 'درخواست‌های خرید چکی'
        ordering = ('-created_at', '-id')
        constraints = [
            models.UniqueConstraint(fields=('user',), condition=Q(status='pending'), name='cheque_credit_one_pending_per_user'),
            models.CheckConstraint(condition=~Q(status='rejected') | Q(rejection_reason__gt=''), name='cheque_credit_reject_needs_reason'),
            models.CheckConstraint(condition=Q(requested_limit__gt=0), name='cheque_credit_requested_limit_gt_0'),
        ]
        indexes = [models.Index(fields=('status', 'created_at'), name='chq_credit_status_created_idx')]

    def __str__(self):
        return f'درخواست خرید چکی #{self.pk} - {self.user_id} ({self.get_status_display()})'

    @property
    def is_pending(self):
        return self.status == self.STATUS_PENDING


def cheque_credit_doc_upload_to(instance, filename):
    """ <سال>/<ماه>/<uuid>.<پسوند از نوع واقعی>؛ نام اصلی کاربر هرگز در مسیر نمی‌آید """
    now = timezone.now()
    return f'{now:%Y}/{now:%m}/{instance.public_id}.{instance.ext}'


class ChequeCreditDocument(models.Model):
    """ مدرک تصویری یک درخواست (دسته‌چک، کارت ملی، ...). همیشه دوباره‌کدشده (بدون EXIF) و بدون نشانی عمومی. """
    KIND_CHEQUE_BOOK = 'cheque_book'
    KIND_NATIONAL_CARD = 'national_card'
    KIND_BUSINESS_LICENSE = 'business_license'
    KIND_OTHER = 'other'
    KIND_CHOICES = (
        (KIND_CHEQUE_BOOK, 'تصویر دسته‌چک'),
        (KIND_NATIONAL_CARD, 'کارت ملی'),
        (KIND_BUSINESS_LICENSE, 'مجوز کسب‌وکار'),
        (KIND_OTHER, 'سایر'),
    )

    request = models.ForeignKey(ChequeCreditRequest, on_delete=models.CASCADE, related_name='documents', verbose_name='درخواست')
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, verbose_name='شناسه‌ی عمومی')
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default=KIND_OTHER, verbose_name='نوع مدرک')
    file = models.FileField(storage=cheque_credit_doc_storage, upload_to=cheque_credit_doc_upload_to, max_length=200,
                            verbose_name='فایل')
    ext = models.CharField(max_length=5, verbose_name='پسوند (از نوع واقعی فایل)')
    content_type = models.CharField(max_length=40, verbose_name='نوع محتوا (از بررسی بایت‌ها)')
    original_name = models.CharField(max_length=80, blank=True, default='', verbose_name='نام اصلی (فقط نمایش)')
    size = models.PositiveIntegerField(default=0, verbose_name='حجم (بایت)')
    width = models.PositiveSmallIntegerField(null=True, blank=True, verbose_name='عرض')
    height = models.PositiveSmallIntegerField(null=True, blank=True, verbose_name='ارتفاع')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='زمان')

    class Meta:
        verbose_name = 'مدرک درخواست خرید چکی'
        verbose_name_plural = 'مدارک درخواست خرید چکی'
        ordering = ('request_id', 'id')

    def __str__(self):
        return f'{self.public_id}'
