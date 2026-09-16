from django.db import models


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
