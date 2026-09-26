"""
هسته‌ی داده‌ای کیف پول داخلی (Wallet Phase 1 — فقط مدل‌ها؛ بدون UI/ویو/URL).

معماری طبق گزارش ممیزی Phase 0 تأیید شده: مستقل از orders.Order و payments.Transaction
(که هر دو مستقیم به فاکتور/حسابداری هلو وصل‌اند)، تا شارژ/برداشت کیف پول هرگز باعث ثبت
سند جعلی در هلو یا مصرف اشتباهی promotions نشود. تمام تغییرات balance/reserved_balance
و افزودن WalletTransaction فقط باید از طریق wallet/services.py انجام شود؛ هیچ کد دیگری
(نه ادمین، نه ویو، نه شل) نباید مستقیم این فیلدها را بنویسد.
"""

from django.db import models
from django.db.models import CheckConstraint, F, Q

from accounts.models import CustomUser


class Wallet(models.Model):
    """
    یک‌به‌یک با کاربر. balance/reserved_balance اینجا صرفاً یک کش هم‌گام‌شده‌اند (منبع حقیقت
    واقعی، مجموع جبری WalletTransaction است - تست‌های این فاز همین تطابق را می‌سنجند)؛
    نگهداری این کش برای خواندن سریع بدون SUM روی کل دفترکل در هر بار مصرف است.

    on_delete=PROTECT (نه CASCADE مثل payments.Transaction.user و نه SET_NULL مثل
    orders.Order.user): این پروژه در دو جای دیگر دو انتخاب متفاوت برای FK کاربر دارد؛
    برای یک دفترکل مالی هیچ‌کدام مناسب نیست - CASCADE سابقه‌ی مالی را نابود می‌کند و
    SET_NULL مالکیت پول را گم می‌کند. PROTECT یعنی تا کاربری تراز/تراکنش دارد، اصلاً
    قابل حذف نیست.
    """
    user = models.OneToOneField(CustomUser, on_delete=models.PROTECT, related_name='wallet', verbose_name='کاربر')
    balance = models.DecimalField(max_digits=12, decimal_places=0, default=0, verbose_name='موجودی کل')
    reserved_balance = models.DecimalField(
        max_digits=12, decimal_places=0, default=0, verbose_name='موجودی بلوکه‌شده',
        help_text='مبالغ درخواست‌های برداشت PENDING؛ تا تصمیم نهایی از available_balance کم است ولی از balance کم نشده.',
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='تاریخ ایجاد')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='آخرین تغییر')

    class Meta:
        verbose_name = 'کیف پول'
        verbose_name_plural = 'کیف پول‌ها'
        constraints = [
            CheckConstraint(condition=Q(balance__gte=0), name='wallet_balance_gte_0'),
            CheckConstraint(condition=Q(reserved_balance__gte=0), name='wallet_reserved_balance_gte_0'),
            # بلوکه هیچ‌وقت نمی‌تواند از کل موجودی بیشتر شود - همان قید available_balance >= 0
            CheckConstraint(condition=Q(reserved_balance__lte=F('balance')), name='wallet_reserved_lte_balance'),
        ]

    def __str__(self):
        return f'کیف پول {self.user.phone_number}'

    @property
    def available_balance(self):
        """ موجودی واقعاً قابل‌خرج/قابل‌درخواست؛ همیشه از این برای چک کافی‌بودن موجودی استفاده شود، نه balance خام """
        return self.balance - self.reserved_balance


class WalletTransaction(models.Model):
    """
    دفترکل کیف پول - غیرقابل‌ویرایش و غیرقابل‌حذف (نگاه کنید save()/delete() پایین).
    اصلاح یک تراکنش همیشه با ثبت یک تراکنش معکوس جدید انجام می‌شود (wallet/services.py:reverse_transaction)،
    نه ویرایش رکورد موجود - دقیقاً همان قرارداد اسنپ‌شات‌محورِ orders.Order برای فاکتور.
    """
    KIND_TOPUP = 'topup'
    KIND_WITHDRAWAL = 'withdrawal'
    KIND_REFUND = 'refund'
    KIND_LOYALTY_REDEEM = 'loyalty_redeem'
    KIND_CART_PAYMENT = 'cart_payment'
    KIND_REVERSAL = 'reversal'
    KIND_CHOICES = (
        (KIND_TOPUP, 'شارژ کیف پول'),
        (KIND_WITHDRAWAL, 'برداشت / تسویه'),
        (KIND_REFUND, 'مرجوعی فاکتور'),
        (KIND_LOYALTY_REDEEM, 'تبدیل امتیاز وفاداری'),
        (KIND_CART_PAYMENT, 'پرداخت سبد خرید با کیف پول'),
        (KIND_REVERSAL, 'تراکنش اصلاحی معکوس'),
    )

    wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name='transactions', verbose_name='کیف پول')
    amount = models.DecimalField(max_digits=12, decimal_places=0, verbose_name='مبلغ (علامت‌دار: مثبت=واریز، منفی=برداشت)')
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, verbose_name='نوع تراکنش')
    # اسنپ‌شات موجودی بلافاصله بعد از این ردیف - برای ممیزی/گزارش سریع بدون نیاز به SUM پنجره‌ای روی کل تاریخچه
    balance_after = models.DecimalField(max_digits=12, decimal_places=0, verbose_name='موجودی پس از این تراکنش')

    # رفرنس باز برای فازهای بعدی (مرجوعی فاکتور، تبدیل امتیاز، پرداخت سبد) بدون نیاز به تغییر هسته‌ی مالی؛
    # عمداً دو ستون ساده (نه GenericForeignKey/ContentType جنگو) - این پروژه جایی از آن استفاده نمی‌کند
    # و دو ستون صریح هم سبک‌تر است هم مستقیم قابل ایندکس/کوئری.
    reference_type = models.CharField(max_length=50, blank=True, default='', verbose_name='نوع مرجع')
    reference_id = models.PositiveIntegerField(null=True, blank=True, verbose_name='شناسه‌ی مرجع')

    description = models.CharField(max_length=255, blank=True, default='', verbose_name='توضیحات')
    created_by = models.ForeignKey(
        CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
        verbose_name='ثبت‌کننده', help_text='کاربر خودش (شارژ/پرداخت) یا ادمین (تسویه‌ی دستی/اصلاح).',
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='تاریخ ثبت')

    class Meta:
        verbose_name = 'تراکنش کیف پول'
        verbose_name_plural = 'تراکنش‌های کیف پول'
        ordering = ('-created_at', '-id')

    def __str__(self):
        return f'{self.get_kind_display()} ({self.amount:+}) - کیف پول #{self.wallet_id}'

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError(
                'رکوردهای دفترکل کیف پول تغییرناپذیرند. برای اصلاح، به‌جای ویرایش این رکورد '
                'از wallet.services.reverse_transaction() برای ثبت یک تراکنش معکوس استفاده کنید.'
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError('رکوردهای دفترکل کیف پول قابل‌حذف نیستند؛ از تراکنش معکوس استفاده کنید.')


class WithdrawalRequest(models.Model):
    """
    درخواست برداشت/تسویه‌ی دستی. اطلاعات بانکی به‌صورت اسنپ‌شات متنی ذخیره می‌شود (نه FK به یک
    مدل «حساب بانکی» کاربر) - دقیقاً همان توجیه orders.Order برای کپی‌کردن آدرس گیرنده در لحظه‌ی
    ثبت: اگر کاربر بعداً شماره کارتش را عوض/حذف کند، درخواست‌های قبلی نباید دست بخورند.
    """
    STATUS_PENDING = 'PENDING'
    STATUS_COMPLETED = 'COMPLETED'
    STATUS_REJECTED = 'REJECTED'
    STATUS_CHOICES = (
        (STATUS_PENDING, 'در انتظار بررسی'),
        (STATUS_COMPLETED, 'تسویه شد'),
        (STATUS_REJECTED, 'رد شد'),
    )

    wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name='withdrawal_requests', verbose_name='کیف پول')
    amount = models.DecimalField(max_digits=12, decimal_places=0, verbose_name='مبلغ درخواستی')

    card_number_snapshot = models.CharField(max_length=25, blank=True, default='', verbose_name='شماره کارت (اسنپ‌شات)')
    iban_snapshot = models.CharField(max_length=34, blank=True, default='', verbose_name='شماره شبا (اسنپ‌شات)')
    account_holder_snapshot = models.CharField(max_length=100, verbose_name='نام صاحب حساب (اسنپ‌شات)')

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING, verbose_name='وضعیت')
    rejection_reason = models.TextField(blank=True, default='', verbose_name='دلیل رد')

    # وقتی COMPLETED شود، دقیقاً همان تراکنشی که services.complete_withdrawal ثبت کرده اینجا لینک می‌شود
    transaction = models.OneToOneField(
        WalletTransaction, on_delete=models.PROTECT, null=True, blank=True,
        related_name='withdrawal_request', verbose_name='تراکنش برداشت ثبت‌شده',
    )

    requested_at = models.DateTimeField(auto_now_add=True, verbose_name='زمان درخواست')
    decided_at = models.DateTimeField(null=True, blank=True, verbose_name='زمان تصمیم‌گیری')
    decided_by = models.ForeignKey(
        CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='+', verbose_name='تصمیم‌گیرنده',
    )

    class Meta:
        verbose_name = 'درخواست برداشت'
        verbose_name_plural = 'درخواست‌های برداشت'
        ordering = ('-requested_at',)
        constraints = [
            CheckConstraint(condition=Q(amount__gt=0), name='withdrawalrequest_amount_gt_0'),
            CheckConstraint(
                condition=~Q(status='REJECTED') | ~Q(rejection_reason=''),
                name='withdrawalrequest_rejected_requires_reason',
            ),
            CheckConstraint(
                condition=~Q(status='COMPLETED') | Q(transaction__isnull=False),
                name='withdrawalrequest_completed_requires_transaction',
            ),
        ]

    def __str__(self):
        return f'درخواست برداشت #{self.pk} ({self.get_status_display()})'


class WalletTopupRequest(models.Model):
    """
    درخواست شارژ آنلاین کیف پول از طریق درگاه آزمایشی (Mock) - عمداً کاملاً مستقل از
    orders.Order/payments.Transaction (نگاه کنید توضیح بالای فایل)؛ ساختار فیلدها آینه‌ی
    payments.Transaction است (authority/status یکتا و idempotent) اما بدون FK به Order.
    """
    STATUS_PENDING = 'pending'
    STATUS_SUCCESS = 'success'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = (
        (STATUS_PENDING, 'در انتظار پرداخت'),
        (STATUS_SUCCESS, 'پرداخت موفق'),
        (STATUS_FAILED, 'پرداخت ناموفق'),
    )

    wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name='topup_requests', verbose_name='کیف پول')
    amount = models.DecimalField(max_digits=12, decimal_places=0, verbose_name='مبلغ درخواستی')
    authority = models.CharField(max_length=100, unique=True, verbose_name='کد ارجاع درگاه (Authority)')
    ref_id = models.CharField(max_length=100, blank=True, default='', verbose_name='شماره پیگیری بانک (RefID)')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING, verbose_name='وضعیت')

    transaction = models.OneToOneField(
        WalletTransaction, on_delete=models.PROTECT, null=True, blank=True,
        related_name='topup_request', verbose_name='تراکنش شارژ ثبت‌شده',
    )

    created_at = models.DateTimeField(auto_now_add=True, verbose_name='تاریخ ایجاد')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='آخرین تغییر')

    class Meta:
        verbose_name = 'درخواست شارژ کیف پول'
        verbose_name_plural = 'درخواست‌های شارژ کیف پول'
        ordering = ('-created_at',)
        constraints = [
            CheckConstraint(condition=Q(amount__gt=0), name='wallettopuprequest_amount_gt_0'),
            CheckConstraint(
                condition=~Q(status='success') | Q(transaction__isnull=False),
                name='wallettopuprequest_success_requires_transaction',
            ),
        ]

    def __str__(self):
        return f'{self.authority} - {self.status}'
