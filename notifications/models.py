from django.db import models


class NotificationSetting(models.Model):
    """
    کنترل ادمین روی هر «نوع پیام» (کلید templates_registry.TEMPLATES): خاموش/روشن کردن کامل
    یک نوع پیام، یا جای‌گزینی متن پیش‌فرض با متن دلخواه - بدون دیپلوی مجدد.

    ردیف‌ها از روی کلیدهای TEMPLATES ساخته می‌شوند (sync_notification_settings، پایین همین فایل: بعد از هر migrate، هنگام
    باز کردن لیست ادمین و هنگام ارسال)؛ به همین دلیل از پنل نمی‌توان ردیف تازه افزود یا حذف کرد
    (notifications/admin.py) - فقط is_enabled/custom_body قابل ویرایش‌اند.

    عنوان فارسی/متن پیش‌فرض/پارامترهای لازم عمداً این‌جا کپی نشده‌اند بلکه هر بار از
    templates_registry.TEMPLATES (منبع واحد حقیقت برای متن پیام‌ها) خوانده می‌شوند - اگر عنوان یا
    متنی در کد عوض شود، بدون هیچ مایگریشنی همین‌جا هم به‌روز است.
    """

    template_key = models.CharField(max_length=64, unique=True, verbose_name='کلید قالب پیام')
    is_enabled = models.BooleanField(default=True, verbose_name='فعال (ارسال شود)')
    custom_body = models.TextField(
        blank=True, verbose_name='متن جای‌گزین',
        help_text='اگر خالی باشد، متن پیش‌فرض تعریف‌شده در کد استفاده می‌شود. پارامترهای لازم '
                   'قالب باید عیناً با همان نام (مثلاً {name}) در متن جای‌گزین هم باشند.',
    )
    updated_at = models.DateTimeField(auto_now=True, verbose_name='آخرین ویرایش')

    class Meta:
        verbose_name = 'تنظیمات نوع پیام'
        verbose_name_plural = 'تنظیمات انواع پیام'
        ordering = ('template_key',)

    def __str__(self):
        return self.title

    @property
    def _template(self):
        from .templates_registry import TEMPLATES
        return TEMPLATES.get(self.template_key)

    @property
    def title(self):
        """ عنوان فارسی گویا برای نمایش در پنل؛ اگر کلید دیگر در کد تعریف نشده باشد، خودِ کلید """
        template = self._template
        return template.title if template else self.template_key

    @property
    def default_body(self):
        """ متن پیش‌فرضی که اگر custom_body خالی باشد واقعاً ارسال می‌شود """
        template = self._template
        return template.body if template else ''

    @property
    def available_variables(self):
        """ نام متغیرهایی که متن جای‌گزین باید عیناً با همین نام‌ها در {} داشته باشد """
        template = self._template
        return template.required if template else ()


def sync_notification_settings():
    """
    برای هر کلید templates_registry.TEMPLATES که ردیف تنظیماتی ندارد یکی می‌سازد (is_enabled طبق default_enabled قالب)،
    تا هیچ پیامی نباشد که ارسال می‌شود ولی در پنل (روشن/خاموش و متن جای‌گزین) دیده نشود. ردیف‌های موجود و تنظیمات ادمین
    دست‌نخورده می‌مانند. بعد از هر migrate، هنگام باز کردن لیست تنظیمات و (برای کلید بی‌ردیف) هنگام ارسال صدا زده می‌شود.
    خروجی: تعداد ردیف‌های تازه.
    """
    from .templates_registry import TEMPLATES

    existing = set(NotificationSetting.objects.values_list('template_key', flat=True))
    missing = [key for key in TEMPLATES if key not in existing]
    for key in missing:
        # get_or_create (نه bulk_create با ignore_conflicts: SQL Server پشتیبانی نمی‌کند)؛ هم‌زمانیِ دو فرایند را هم تحمل می‌کند
        NotificationSetting.objects.get_or_create(template_key=key, defaults={'is_enabled': TEMPLATES[key].default_enabled})
    return len(missing)


class Notification(models.Model):
    """
    لاگ ماندگار هر پیام خروجی سایت.

    نسخه‌ی قبلی (services/sms.py) هیچ ردی از پیام‌ها نگه نمی‌داشت: اگر ارسال شکست می‌خورد،
    فقط یک خط لاگ می‌ماند و پیام برای همیشه گم می‌شد. با این جدول هر پیام حالتی دارد، قابل
    تلاش مجدد است، و در پنل ادمین قابل پیگیری.
    """

    STATUS_PENDING = 'pending'
    STATUS_SENT = 'sent'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = (
        (STATUS_PENDING, 'در صف ارسال'),
        (STATUS_SENT, 'ارسال شد'),
        (STATUS_FAILED, 'ناموفق'),
    )

    recipient = models.CharField(max_length=190, db_index=True, verbose_name='مقصد')
    template_key = models.CharField(max_length=64, verbose_name='نوع پیام')
    text = models.TextField(verbose_name='متن ارسالی')
    # متغیرهای خام قالب (قبل از رندر شدن به متن)؛ بک‌اندهای الگو-محور مثل ملی‌پیامک که به
    # bodyId متکی‌اند و متن نهایی را قبول نمی‌کنند، به همین‌ها نیاز دارند نه به text
    context = models.JSONField(default=dict, blank=True, verbose_name='داده‌های قالب')
    # وقتی فراخوان‌کننده صریحاً کانال را انتخاب کرده (نه سرویس فعال سراسری سایت) — مثلاً
    # کاربر برای اطلاع موجودی «ایمیل» را انتخاب کرده در حالی که سرویس فعال سایت پیامک است.
    # خالی یعنی طبق معمول از سرویس فعال تنظیمات سایت استفاده شود.
    backend_override = models.CharField(max_length=190, blank=True, verbose_name='بک‌اند اختصاصی این پیام')

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True, verbose_name='وضعیت')
    attempts = models.PositiveSmallIntegerField(default=0, verbose_name='تعداد تلاش')
    backend = models.CharField(max_length=190, blank=True, verbose_name='موتور ارسال')
    provider_message_id = models.CharField(max_length=190, blank=True, verbose_name='شناسه نزد سرویس')
    error = models.TextField(blank=True, verbose_name='آخرین خطا')

    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='تاریخ ایجاد')
    sent_at = models.DateTimeField(null=True, blank=True, verbose_name='تاریخ ارسال')

    class Meta:
        verbose_name = 'پیام خروجی'
        verbose_name_plural = 'پیام‌های خروجی'
        ordering = ('-created_at',)
        indexes = [models.Index(fields=['status', 'created_at'])]

    def __str__(self):
        return f"{self.get_status_display()} | {self.template_key} -> {self.recipient}"
