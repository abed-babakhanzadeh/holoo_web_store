"""
مدل‌های مرجوعی کالا (Phase 1 - بخش اول: فقط ساختار داده و ماشین وضعیت؛ سرویس‌های تغییر
وضعیت و محاسبه‌ی ریفاند در بخش دوم می‌آیند).

معماری: دقیقاً هم‌الگوی wallet.WithdrawalRequest (Phase 3) - یک فیلد status با گذارهای مجاز
که فقط از لایه‌ی سرویس (نه ادمین مستقیم) قابل تغییرند، اسنپ‌شات اطلاعات بانکی (نه FK زنده به
UserBankAccount)، و timestamp جدا برای هر مرحله.
"""

from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator
from django.db import models
from django.db.models import CheckConstraint, F, Q

from accounts.models import CustomUser
from products.models import Category, Product, SiteSettings


class ReturnReason(models.Model):
    """ دلیل مرجوعی (دیکشنری ادمین) - جایگزین گزینه‌های هاردکد قالب آرینو step-two """
    title = models.CharField(max_length=200, verbose_name='عنوان')

    PAYER_CUSTOMER = 'customer'
    PAYER_STORE = 'store'
    PAYER_CHOICES = (
        (PAYER_CUSTOMER, 'مشتری'),
        (PAYER_STORE, 'فروشگاه'),
    )
    shipping_cost_payer = models.CharField(
        max_length=10, choices=PAYER_CHOICES, default=PAYER_CUSTOMER, verbose_name='پرداخت‌کننده‌ی هزینه‌ی پست برگشت',
    )
    # مثلاً برای دلیل «سایر» - Part C.1: ReturnStepTwoForm فیلد توضیح را وقتی دلیلِ انتخاب‌شده
    # این پرچم را دارد اجباری می‌کند
    requires_description = models.BooleanField(
        default=False, verbose_name='توضیح برای این دلیل اجباری باشد',
        help_text='مثلاً برای دلیل «سایر»؛ روشن باشد یعنی مشتری باید حتماً توضیح بنویسد.',
    )
    is_active = models.BooleanField(default=True, verbose_name='فعال (در فرم نمایش داده شود)')
    order = models.PositiveIntegerField(default=0, verbose_name='ترتیب نمایش')

    class Meta:
        verbose_name = 'دلیل مرجوعی'
        verbose_name_plural = 'دلایل مرجوعی'
        ordering = ('order', 'id')

    def __str__(self):
        return self.title


class ReturnabilityRule(models.Model):
    """
    قاعده‌ی «این محصول/دسته قابل مرجوع نیست» - دقیقاً هم‌الگوی promotions.Promotion.scope
    (products.models.py:ManyToMany به Product/Category؛ دسته شامل همه‌ی زیردسته‌ها با
    Category.get_descendant_ids می‌شود، نه فقط تطبیق مستقیم).
    """
    SCOPE_PRODUCT = 'product'
    SCOPE_CATEGORY = 'category'
    SCOPE_CHOICES = (
        (SCOPE_PRODUCT, 'محصولات مشخص'),
        (SCOPE_CATEGORY, 'دسته‌بندی (شامل زیردسته‌ها)'),
    )
    scope = models.CharField(max_length=10, choices=SCOPE_CHOICES, verbose_name='شمول قاعده')
    products = models.ManyToManyField(Product, blank=True, related_name='+', verbose_name='محصولات مشمول')
    categories = models.ManyToManyField(
        Category, blank=True, related_name='+', verbose_name='دسته‌های مشمول', help_text='شامل همه‌ی زیردسته‌ها.',
    )
    reason_text = models.CharField(
        max_length=300, verbose_name='متن نمایش به کاربر',
        help_text='مثلاً «این کالا به دلیل بهداشتی قابل مرجوع نیست».',
    )
    is_active = models.BooleanField(default=True, verbose_name='فعال')

    class Meta:
        verbose_name = 'قاعده‌ی عدم امکان مرجوعی'
        verbose_name_plural = 'قواعد عدم امکان مرجوعی'

    def __str__(self):
        return self.reason_text

    @classmethod
    def find_blocking_rule(cls, product):
        """
        اولین قاعده‌ی فعالِ مسدودکننده برای این محصول، یا None اگر قابل‌مرجوع است.

        اولویت: قواعد SCOPE_PRODUCT همیشه قبل از قواعد SCOPE_CATEGORY بررسی می‌شوند - چون
        این‌جا فقط قاعده‌ی «مسدودکننده» داریم (هیچ قاعده‌ی «همیشه قابل‌مرجوع/استثنا» وجود ندارد)،
        این ترتیب فقط تعیین می‌کند کدام reason_text/shipping_cost_payer به کاربر نشان داده شود؛
        نتیجه‌ی نهایی (مسدود بودن) در هر دو حالت یکی است. اگر چند قاعده در همان scope مطابقت
        داشته باشند، قدیمی‌ترین (کمترین pk) برنده است - نه ترتیب دلبخواهیِ دیتابیس.
        """
        rules = list(cls.objects.filter(is_active=True).order_by('pk').prefetch_related('products', 'categories'))

        for rule in rules:
            if rule.scope == cls.SCOPE_PRODUCT and rule.products.filter(pk=product.pk).exists():
                return rule

        if not product.category_id:
            return None
        for rule in rules:
            if rule.scope != cls.SCOPE_CATEGORY:
                continue
            blocked_category_ids = set()
            for category in rule.categories.all():
                blocked_category_ids.update(category.get_descendant_ids())
            if product.category_id in blocked_category_ids:
                return rule
        return None


class ReturnRequest(models.Model):
    """
    درخواست مرجوعی یک سفارش. چرخه‌ی حیات (هم‌الگوی wallet.WithdrawalRequest):
    PENDING -> APPROVED -> ITEM_RECEIVED -> REFUND_PENDING -> COMPLETED
    رد (REJECTED) از هر یک از ۴ وضعیت فعال مجاز است؛ از COMPLETED هرگز.
    """
    STATUS_PENDING = 'PENDING'
    STATUS_APPROVED = 'APPROVED'
    STATUS_ITEM_RECEIVED = 'ITEM_RECEIVED'
    STATUS_REFUND_PENDING = 'REFUND_PENDING'
    STATUS_COMPLETED = 'COMPLETED'
    STATUS_REJECTED = 'REJECTED'
    STATUS_CHOICES = (
        (STATUS_PENDING, 'در انتظار بررسی'),
        (STATUS_APPROVED, 'تأییدشده (در انتظار ارسال کالا توسط مشتری)'),
        (STATUS_ITEM_RECEIVED, 'کالا دریافت شد (در انتظار بازرسی نهایی)'),
        (STATUS_REFUND_PENDING, 'در صف بازپرداخت'),
        (STATUS_COMPLETED, 'بازپرداخت تکمیل شد'),
        (STATUS_REJECTED, 'رد شد'),
    )
    # وضعیت‌هایی که ظرفیت مرجوعی OrderItem را «رزروشده» نگه می‌دارند (نگاه کنید بخش دوم:
    # refund_calculator.get_returnable_quantity) - این‌جا فقط برای مرجع/مستندسازی مشترک است
    ACTIVE_STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_ITEM_RECEIVED, STATUS_REFUND_PENDING, STATUS_COMPLETED)

    REFUND_WALLET = 'wallet'
    REFUND_BANK = 'bank'
    REFUND_METHOD_CHOICES = (
        (REFUND_WALLET, 'شارژ کیف‌پول'),
        (REFUND_BANK, 'واریز دستی به حساب بانکی'),
    )

    order = models.ForeignKey('orders.Order', on_delete=models.PROTECT, related_name='return_requests', verbose_name='سفارش')
    user = models.ForeignKey(CustomUser, on_delete=models.PROTECT, related_name='return_requests', verbose_name='کاربر')
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default=STATUS_PENDING, verbose_name='وضعیت')

    refund_method = models.CharField(max_length=10, choices=REFUND_METHOD_CHOICES, verbose_name='روش بازپرداخت')
    # اسنپ‌شات - نه FK زنده به UserBankAccount؛ دقیقاً هم‌دلیل WithdrawalRequest.card_number_snapshot
    # (Phase 3): اگر کاربر بعداً حساب را حذف/ویرایش کند، درخواست‌های قبلی نباید دست بخورند.
    bank_account = models.ForeignKey(
        'accounts.UserBankAccount', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='+', verbose_name='حساب بانکی انتخاب‌شده (در لحظه‌ی ثبت)',
    )
    bank_account_holder_snapshot = models.CharField(max_length=100, blank=True, verbose_name='نام صاحب حساب (اسنپ‌شات)')
    bank_card_snapshot = models.CharField(max_length=16, blank=True, verbose_name='شماره کارت (اسنپ‌شات)')
    bank_iban_snapshot = models.CharField(max_length=26, blank=True, verbose_name='شماره شبا (اسنپ‌شات)')

    wallet_transaction = models.OneToOneField(
        'wallet.WalletTransaction', null=True, blank=True, on_delete=models.PROTECT,
        related_name='return_request', verbose_name='تراکنش شارژ کیف‌پول ثبت‌شده',
    )
    holoo_return_note = models.CharField(
        max_length=200, blank=True, verbose_name='یادداشت سند برگشت‌ازفروش هلو',
        help_text='فقط برای ردیابی دستی حسابداری؛ سایت هیچ سندی در هلو ثبت نمی‌کند.',
    )

    shipping_refunded = models.BooleanField(default=False, verbose_name='هزینه‌ی ارسال هم بازگردانده شد')
    # اسنپ‌شات قطعی مبلغ کرایه‌ی مسترد‌شده (در لحظه‌ی گذار به REFUND_PENDING نوشته می‌شود، نگاه
    # کنید returns/services.py:mark_refund_pending) - وابسته به order.shipping_cost زنده نیست
    # تا اگر بعداً چیزی در سفارش عوض شد، مبلغ ثبت‌شده‌ی این مرجوعی هرگز تغییر نکند.
    shipping_refund_amount = models.DecimalField(
        max_digits=12, decimal_places=0, default=0, verbose_name='مبلغ کرایه‌ی مسترد‌شده',
    )
    rejection_reason = models.TextField(blank=True, verbose_name='دلیل رد')

    requested_at = models.DateTimeField(auto_now_add=True, verbose_name='زمان ثبت درخواست')
    decided_at = models.DateTimeField(null=True, blank=True, verbose_name='زمان تصمیم (تأیید/رد)')
    decided_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='+', verbose_name='تصمیم‌گیرنده')
    item_received_at = models.DateTimeField(null=True, blank=True, verbose_name='زمان دریافت کالا')
    received_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='+', verbose_name='ثبت‌کننده‌ی دریافت کالا')
    completed_at = models.DateTimeField(null=True, blank=True, verbose_name='زمان تکمیل بازپرداخت')
    completed_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='+', verbose_name='ثبت‌کننده‌ی تکمیل بازپرداخت')

    class Meta:
        verbose_name = 'درخواست مرجوعی'
        verbose_name_plural = 'درخواست‌های مرجوعی'
        ordering = ('-requested_at',)
        constraints = [
            CheckConstraint(condition=~Q(status='REJECTED') | ~Q(rejection_reason=''), name='returnrequest_rejected_requires_reason'),
            CheckConstraint(
                condition=~Q(refund_method='bank') | Q(bank_card_snapshot__gt='') | Q(bank_iban_snapshot__gt=''),
                name='returnrequest_bank_refund_requires_account',
            ),
            CheckConstraint(condition=~Q(status='COMPLETED') | Q(completed_at__isnull=False), name='returnrequest_completed_requires_timestamp'),
        ]

    def __str__(self):
        return f'مرجوعی #{self.pk} - سفارش #{self.order_id}'

    @property
    def total_refund_amount(self):
        """
        جمع اسنپ‌شات‌های قطعی (item.refund_amount ها + shipping_refund_amount)؛ همه‌ی این‌ها
        فقط در لحظه‌ی گذار به REFUND_PENDING نوشته می‌شوند (نگاه کنید
        returns/services.py:mark_refund_pending) و بعد از آن دیگر تغییر نمی‌کنند - این پروپرتی
        صرفاً جمعِ همان اعداد ثابت است، نه یک محاسبه‌ی زنده‌ی وابسته به وضعیت فعلیِ سفارش.
        """
        items_total = sum((item.refund_amount or 0 for item in self.items.all()), 0)
        return items_total + self.shipping_refund_amount


class ReturnItem(models.Model):
    """
    یک قلم از یک OrderItem داخل یک ReturnRequest. تفکیک requested_quantity/approved_quantity
    طبق تصمیم صریح کارفرما: مبلغ نهایی همیشه بر مبنای approved_quantity محاسبه می‌شود (نه
    requested_quantity)، چون بازرسی فیزیکی ممکن است بخشی از خواسته‌ی مشتری را رد کند.
    """
    return_request = models.ForeignKey(ReturnRequest, on_delete=models.CASCADE, related_name='items', verbose_name='درخواست مرجوعی')
    order_item = models.ForeignKey('orders.OrderItem', on_delete=models.PROTECT, related_name='return_items', verbose_name='قلم سفارش')
    reason = models.ForeignKey(ReturnReason, on_delete=models.PROTECT, related_name='+', verbose_name='دلیل مرجوعی')

    requested_quantity = models.PositiveIntegerField(verbose_name='تعداد درخواستی')
    approved_quantity = models.PositiveIntegerField(null=True, blank=True, verbose_name='تعداد تأییدشده پس از بازرسی')
    refund_amount = models.DecimalField(
        max_digits=12, decimal_places=0, null=True, blank=True, verbose_name='مبلغ ریفاند این قلم',
        help_text='بر مبنای approved_quantity محاسبه می‌شود (نگاه کنید returns.refund_calculator - بخش دوم).',
    )
    description = models.TextField(blank=True, verbose_name='توضیحات مشتری')

    class Meta:
        verbose_name = 'قلم مرجوعی'
        verbose_name_plural = 'اقلام مرجوعی'
        constraints = [
            CheckConstraint(condition=Q(requested_quantity__gt=0), name='returnitem_requested_qty_gt_0'),
            CheckConstraint(
                condition=Q(approved_quantity__isnull=True) | Q(approved_quantity__lte=F('requested_quantity')),
                name='returnitem_approved_lte_requested',
            ),
        ]

    def __str__(self):
        return f'{self.order_item} × {self.requested_quantity}'


def _return_attachment_upload_path(instance, filename):
    return f'returns/attachments/{instance.return_item.return_request_id}/{filename}'


class ReturnAttachment(models.Model):
    """
    مدرک تصویری/ویدئویی پیوست‌شده به یک قلم مرجوعی (حداکثر ۵ فایل به‌ازای هر ReturnItem -
    هم‌الگوی ReviewImage: سقف در سطح سرویس/فرم چک می‌شود، نه قید دیتابیس، چون قید دیتابیسی
    روی «تعداد ردیف‌های مرتبط» در جنگو/MSSQL مستقیم ممکن نیست).
    """
    IMAGE = 'image'
    VIDEO = 'video'
    TYPE_CHOICES = ((IMAGE, 'عکس'), (VIDEO, 'فیلم'))

    MAX_PER_ITEM = 5   # سقف تعداد فایل - ثابت، برخلاف سقف حجم، از پنل قابل تغییر نیست
    # مقادیر پیش‌فرض/مرجع - منبع واقعی حقیقت SiteSettings.return_attachment_max_*_mb است
    # (نگاه کنید clean() پایین)؛ این دو فقط برای مستندسازی و سازگاری تست‌های قدیمی نگه داشته شده‌اند
    MAX_IMAGE_SIZE_MB = 5
    MAX_VIDEO_SIZE_MB = 50

    return_item = models.ForeignKey(ReturnItem, on_delete=models.CASCADE, related_name='attachments', verbose_name='قلم مرجوعی')
    file = models.FileField(
        upload_to=_return_attachment_upload_path, verbose_name='فایل',
        validators=[FileExtensionValidator(['jpg', 'jpeg', 'png', 'webp', 'mp4', 'webm', 'mov'])],
    )
    attachment_type = models.CharField(max_length=10, choices=TYPE_CHOICES, verbose_name='نوع فایل')
    original_filename = models.CharField(max_length=255, blank=True, verbose_name='نام اصلی فایل')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='زمان آپلود')

    class Meta:
        verbose_name = 'مدرک مرجوعی'
        verbose_name_plural = 'مدارک مرجوعی'
        ordering = ('created_at', 'id')

    def __str__(self):
        return self.original_filename or f'مدرک #{self.pk}'

    def clean(self):
        errors = {}
        if self.file:
            settings_obj = SiteSettings.cached()
            max_mb = (
                settings_obj.return_attachment_max_image_mb if self.attachment_type == self.IMAGE
                else settings_obj.return_attachment_max_video_mb
            )
            if self.file.size > max_mb * 1024 * 1024:
                errors['file'] = f'حجم فایل نباید از {max_mb} مگابایت بیشتر باشد.'
        if self.return_item_id:
            existing = ReturnAttachment.objects.filter(return_item_id=self.return_item_id).exclude(pk=self.pk).count()
            if existing >= self.MAX_PER_ITEM:
                errors['return_item'] = f'حداکثر {self.MAX_PER_ITEM} مدرک برای هر قلم مجاز است.'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        """
        clean() جنگو به‌خودی‌خود توسط save() صدا زده نمی‌شود (فقط ModelForm.is_valid() آن را
        می‌زند)؛ این‌جا صریح صدا زده می‌شود تا سقف ۵ فایل/سقف حجم از هر مسیری (فرم، سرویس، شل،
        import آینده) رد شود، نه فقط وقتی فراخوان‌کننده یادش بماند clean() را دستی صدا بزند.
        """
        self.clean()
        super().save(*args, **kwargs)

    @classmethod
    def remaining_slots(cls, return_item):
        """ چند مدرک دیگر می‌توان برای این قلم آپلود کرد - برای چک سریع سمت فرم/ویو """
        used = cls.objects.filter(return_item=return_item).count()
        return max(0, cls.MAX_PER_ITEM - used)
