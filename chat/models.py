"""
مدل‌های گفتگوی آنلاین (فاز ۲). قواعد SQL Server: هیچ ستون یکتایی NULL نمی‌شود؛ شمارنده‌ها با F()؛ بدون ignore_conflicts.

ترتیب پیام‌ها با seq است (شماره‌ی ترتیبیِ هر گفتگو)، نه id: seq داخل تراکنشی تخصیص می‌یابد که اول ردیف گفتگو را قفل می‌کند، پس seq
دقیقاً به ترتیب commit است و پولینگِ «after=<seq>» هرگز پیامی را جا نمی‌اندازد (جزئیات: chat/conversations.py).
"""
import uuid

from django.conf import settings
from django.db import models

from .statemachine import ACTIVE, CLOSED, OFFLINE, WAITING_CUSTOMER, WAITING_OPERATOR
from .storage import chat_attachment_storage


class Conversation(models.Model):
    STATUS_CHOICES = (
        (WAITING_OPERATOR, 'در انتظار کارشناس'),
        (ACTIVE, 'در جریان'),
        (WAITING_CUSTOMER, 'در انتظار مشتری'),
        (OFFLINE, 'آفلاین (ناهمزمان)'),
        (CLOSED, 'بسته'),
    )
    CHANNEL_LIVE = 'live'
    CHANNEL_OFFLINE = 'offline'
    CHANNEL_CHOICES = ((CHANNEL_LIVE, 'گفتگوی زنده'), (CHANNEL_OFFLINE, 'پیام آفلاین'))   # مقدار رزرو آینده: 'ai'

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, verbose_name='شناسه‌ی عمومی')
    visitor_hash = models.CharField(max_length=64, db_index=True, blank=True, default='', verbose_name='هش کوکی بازدیدکننده')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                             related_name='chat_conversations', verbose_name='کاربر')
    guest_name = models.CharField(max_length=80, blank=True, default='', verbose_name='نام (مهمان)')
    guest_phone = models.CharField(max_length=11, blank=True, default='', verbose_name='موبایل (مهمان؛ فقط داده‌ی کمکی)')
    guest_phone_verified = models.BooleanField(default=False, verbose_name='موبایل مهمان تأیید شده')

    channel_origin = models.CharField(max_length=8, choices=CHANNEL_CHOICES, default=CHANNEL_OFFLINE, verbose_name='کانال شروع')
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=OFFLINE, verbose_name='وضعیت')
    assigned_operator = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                          related_name='chat_assigned', verbose_name='کارشناس مسئول')

    last_message_seq = models.PositiveIntegerField(default=0, verbose_name='آخرین شماره‌ی پیام')
    last_read_seq_by_customer = models.PositiveIntegerField(default=0, verbose_name='خوانده‌شده توسط مشتری تا')
    last_read_seq_by_operator = models.PositiveIntegerField(default=0, verbose_name='خوانده‌شده توسط کارشناس تا')
    unread_for_operator = models.PositiveIntegerField(default=0, verbose_name='خوانده‌نشده برای کارشناس')
    unread_for_customer = models.PositiveIntegerField(default=0, verbose_name='خوانده‌نشده برای مشتری')

    created_at = models.DateTimeField(auto_now_add=True, verbose_name='ایجاد')
    last_message_at = models.DateTimeField(null=True, blank=True, verbose_name='آخرین پیام')
    last_activity_at = models.DateTimeField(null=True, blank=True, verbose_name='آخرین فعالیت')
    closed_at = models.DateTimeField(null=True, blank=True, verbose_name='زمان بسته شدن')
    next_timer_at = models.DateTimeField(null=True, blank=True, db_index=True, verbose_name='موعد تایمر بعدی')
    next_timer_kind = models.CharField(max_length=16, blank=True, default='', verbose_name='نوع تایمر')

    last_message_preview = models.CharField(max_length=140, blank=True, default='', verbose_name='پیش‌نمایش آخرین پیام')
    last_message_sender = models.CharField(max_length=10, blank=True, default='', verbose_name='فرستنده‌ی آخرین پیام')
    source_path = models.CharField(max_length=300, blank=True, default='', verbose_name='صفحه‌ی شروع')
    client_ip_trunc = models.CharField(max_length=45, blank=True, default='', verbose_name='IP کوتاه‌شده')

    class Meta:
        verbose_name = 'گفتگو'
        verbose_name_plural = 'گفتگوها'
        permissions = (('operate_chat', 'می‌تواند گفتگوهای پشتیبانی را ببیند و پاسخ دهد'),)
        indexes = [
            models.Index(fields=['status', '-last_message_at'], name='chat_conv_status_last_idx'),
            models.Index(fields=['user', '-last_message_at'], name='chat_conv_user_last_idx'),
            models.Index(fields=['assigned_operator', 'status'], name='chat_conv_assignee_idx'),
        ]
        ordering = ('-last_message_at', '-id')

    def __str__(self):
        return f'گفتگو #{self.pk} ({self.get_status_display()})'

    @property
    def is_open(self):
        return self.status != CLOSED

    @property
    def display_name(self):
        if self.user_id:
            name = f'{self.user.first_name or ""} {self.user.last_name or ""}'.strip()
            return name or self.user.phone_number
        return self.guest_name or 'مهمان'

    @property
    def display_phone(self):
        return self.user.phone_number if self.user_id else self.guest_phone


class ChatMessage(models.Model):
    SENDER_CUSTOMER = 'customer'
    SENDER_OPERATOR = 'operator'
    SENDER_SYSTEM = 'system'
    SENDER_CHOICES = ((SENDER_CUSTOMER, 'مشتری'), (SENDER_OPERATOR, 'کارشناس'), (SENDER_SYSTEM, 'سیستم'))   # رزرو: 'ai'
    KIND_TEXT = 'text'
    KIND_EVENT = 'event'

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='messages', verbose_name='گفتگو')
    seq = models.PositiveIntegerField(verbose_name='شماره‌ی ترتیبی در گفتگو')
    sender_type = models.CharField(max_length=10, choices=SENDER_CHOICES, verbose_name='فرستنده')
    operator = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name='+', verbose_name='کارشناس')
    body = models.TextField(verbose_name='متن')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='زمان')
    client_msg_id = models.UUIDField(default=uuid.uuid4, verbose_name='شناسه‌ی سمت کلاینت (ضد تکرار)')
    is_internal_note = models.BooleanField(default=False, verbose_name='یادداشت داخلی (فقط کارشناسان)')
    kind = models.CharField(max_length=8, default=KIND_TEXT, verbose_name='نوع')
    attachments_count = models.PositiveSmallIntegerField(default=0, verbose_name='تعداد پیوست‌ها')

    class Meta:
        verbose_name = 'پیام گفتگو'
        verbose_name_plural = 'پیام‌های گفتگو'
        ordering = ('conversation_id', 'seq')
        constraints = [
            models.UniqueConstraint(fields=['conversation', 'seq'], name='chat_msg_conv_seq_uniq'),
            models.UniqueConstraint(fields=['conversation', 'client_msg_id'], name='chat_msg_conv_client_uniq'),
        ]

    def __str__(self):
        return f'#{self.conversation_id}/{self.seq}'


class ChatEvent(models.Model):
    """ لاگ رخدادهای چرخه‌ی گفتگو (انتقال وضعیت، اختصاص، اتصال مهمان به حساب، ...) برای ممیزی و دیباگ ماشین وضعیت """
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='events', verbose_name='گفتگو')
    type = models.CharField(max_length=32, verbose_name='نوع رخداد')
    actor_type = models.CharField(max_length=10, verbose_name='عامل')
    actor_id = models.IntegerField(null=True, blank=True, verbose_name='شناسه‌ی عامل')
    from_status = models.CharField(max_length=16, blank=True, default='', verbose_name='از وضعیت')
    to_status = models.CharField(max_length=16, blank=True, default='', verbose_name='به وضعیت')
    meta = models.JSONField(default=dict, blank=True, verbose_name='جزئیات')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='زمان')

    class Meta:
        verbose_name = 'رخداد گفتگو'
        verbose_name_plural = 'رخدادهای گفتگو'
        ordering = ('conversation_id', 'id')
        indexes = [models.Index(fields=['conversation', 'id'], name='chat_event_conv_idx')]

    def __str__(self):
        return f'{self.type} #{self.conversation_id}'


class QuickReply(models.Model):
    title = models.CharField(max_length=80, verbose_name='عنوان')
    body = models.TextField(verbose_name='متن پاسخ', help_text='می‌توانید {customer_name} بنویسید؛ هنگام استفاده با نام مشتری جایگزین می‌شود.')
    shortcut = models.CharField(max_length=30, blank=True, default='', verbose_name='میان‌بر', help_text='مثلاً «ship» (کارشناس در پاسخ‌گویی با تایپ / آن را پیدا می‌کند).')
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE, related_name='+',
                              verbose_name='مالک', help_text='خالی = پاسخ عمومی برای همه‌ی کارشناسان؛ وگرنه فقط برای همین کارشناس.')
    is_active = models.BooleanField(default=True, verbose_name='فعال')
    sort_order = models.PositiveSmallIntegerField(default=0, verbose_name='ترتیب نمایش')
    usage_count = models.PositiveIntegerField(default=0, verbose_name='تعداد استفاده')

    class Meta:
        verbose_name = 'پاسخ آماده'
        verbose_name_plural = 'پاسخ‌های آماده'
        ordering = ('sort_order', 'title')

    def __str__(self):
        return self.title


class OperatorPresence(models.Model):
    """ کلید دستی «آنلاین/آفلاین» هر کارشناس و پشتیبانِ حضور لحظه‌ای (حقیقت لحظه‌ای در Redis است؛ فاز ۳) """
    operator = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='chat_presence', verbose_name='کارشناس')
    is_online = models.BooleanField(default=False, verbose_name='خود را آنلاین اعلام کرده')
    last_seen = models.DateTimeField(null=True, blank=True, verbose_name='آخرین نبض')

    class Meta:
        verbose_name = 'حضور کارشناس'
        verbose_name_plural = 'حضور کارشناسان'

    def __str__(self):
        return str(self.operator_id)


def attachment_upload_to(instance, filename):
    """ chat_attachments/<سال>/<ماه>/<uuid>.<پسوند از نوع واقعی>؛ نام اصلی کاربر هرگز در مسیر نمی‌آید """
    from django.utils import timezone

    now = timezone.now()
    return f'{now:%Y}/{now:%m}/{instance.public_id}.{instance.ext}'


class ChatAttachment(models.Model):
    KIND_IMAGE = 'image'
    KIND_PDF = 'pdf'
    KIND_CHOICES = ((KIND_IMAGE, 'تصویر'), (KIND_PDF, 'PDF'))

    message = models.ForeignKey(ChatMessage, on_delete=models.CASCADE, related_name='attachments', verbose_name='پیام')
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, verbose_name='شناسه‌ی عمومی')
    file = models.FileField(storage=chat_attachment_storage, upload_to=attachment_upload_to, max_length=200, verbose_name='فایل')
    kind = models.CharField(max_length=8, choices=KIND_CHOICES, verbose_name='نوع')
    ext = models.CharField(max_length=5, verbose_name='پسوند (از نوع واقعی فایل)')
    content_type = models.CharField(max_length=40, verbose_name='نوع محتوا (از بررسی بایت‌ها)')
    original_name = models.CharField(max_length=80, blank=True, default='', verbose_name='نام اصلی (فقط نمایش)')
    size = models.PositiveIntegerField(default=0, verbose_name='حجم (بایت)')
    width = models.PositiveSmallIntegerField(null=True, blank=True, verbose_name='عرض')
    height = models.PositiveSmallIntegerField(null=True, blank=True, verbose_name='ارتفاع')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='زمان')

    class Meta:
        verbose_name = 'پیوست گفتگو'
        verbose_name_plural = 'پیوست‌های گفتگو'
        ordering = ('message_id', 'id')

    def __str__(self):
        return f'{self.kind}:{self.public_id}'


class ChatBlock(models.Model):
    """
    مسدودسازی بازدیدکننده‌ی مزاحم. بر پایه‌ی کوکی بازدیدکننده (visitor_hash) و/یا حساب کاربری؛ IP عمداً معیار نیست (IPهای مشترک
    دیگران را هم قفل می‌کند). مسدود نمی‌تواند پیام تازه بفرستد یا گفتگو بسازد؛ فقط تاریخچه‌ی خودش را می‌بیند.
    """
    visitor_hash = models.CharField(max_length=64, blank=True, default='', db_index=True, verbose_name='هش کوکی بازدیدکننده')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE,
                             related_name='chat_blocks', verbose_name='کاربر')
    conversation = models.ForeignKey(Conversation, null=True, blank=True, on_delete=models.SET_NULL, related_name='blocks',
                                     verbose_name='گفتگوی مبدأ')
    reason = models.CharField(max_length=200, blank=True, default='', verbose_name='دلیل (فقط برای کارشناسان)')
    blocked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
                                   verbose_name='مسدودکننده')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='زمان مسدودسازی')
    expires_at = models.DateTimeField(null=True, blank=True, verbose_name='پایان مسدودیت', help_text='خالی = دائمی')
    is_active = models.BooleanField(default=True, verbose_name='فعال')

    class Meta:
        verbose_name = 'مسدودسازی گفتگو'
        verbose_name_plural = 'مسدودسازی‌های گفتگو'
        ordering = ('-id',)
        indexes = [models.Index(fields=['is_active', 'visitor_hash'], name='chat_block_active_vh_idx')]

    def __str__(self):
        return f'مسدود #{self.pk}'
