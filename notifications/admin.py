from django.contrib import admin, messages
from django.utils.html import format_html, format_html_join

from .models import Notification, NotificationSetting


@admin.register(NotificationSetting)
class NotificationSettingAdmin(admin.ModelAdmin):
    """
    ردیف‌ها فقط با دیتا-مایگریشن ساخته می‌شوند (یک ردیف به ازای هر کلید در
    templates_registry.TEMPLATES)؛ از پنل نمی‌توان ردیف اضافه/حذف کرد تا کلیدها با کد هم‌سو بمانند.
    """
    list_display = ('title_display', 'template_key', 'is_enabled', 'has_custom_body', 'updated_at')
    list_filter = ('is_enabled',)
    list_editable = ('is_enabled',)
    search_fields = ('template_key', 'custom_body')
    fields = (
        'template_key', 'title_display', 'is_enabled', 'default_body_display', 'variables_guide',
        'custom_body', 'updated_at',
    )
    readonly_fields = ('template_key', 'title_display', 'default_body_display', 'variables_guide', 'updated_at')

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(boolean=True, description='متن جای‌گزین دارد')
    def has_custom_body(self, obj):
        return bool(obj.custom_body)

    @admin.display(description='عنوان پیامک')
    def title_display(self, obj):
        return obj.title

    @admin.display(description='متن پیش‌فرض سامانه')
    def default_body_display(self, obj):
        return format_html(
            '<div style="white-space:pre-wrap;padding:8px 12px;background:#f8f9fa;'
            'border:1px solid #e0e0e0;border-radius:6px;max-width:600px">{}</div>',
            obj.default_body,
        )

    @admin.display(description='متغیرهای در دسترس')
    def variables_guide(self, obj):
        if not obj.available_variables:
            return 'این نوع پیام متغیری ندارد؛ متن جای‌گزین می‌تواند ثابت باشد.'
        chips = format_html_join(
            '، ', '<code style="background:#eef2ff;padding:2px 6px;border-radius:4px">{{{}}}</code>',
            ((v,) for v in obj.available_variables),
        )
        return format_html(
            'اگر متن جای‌گزین بنویسید، باید دقیقاً همین متغیرها را (با همین نام، داخل آکولاد) در آن '
            'بیاورید وگرنه پیام اصلاً ارسال نمی‌شود: {}', chips,
        )


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'template_key', 'recipient', 'status', 'attempts', 'sent_at')
    list_filter = ('status', 'template_key', 'created_at')
    search_fields = ('recipient', 'text', 'provider_message_id')
    readonly_fields = (
        'recipient', 'template_key', 'text', 'context', 'backend_override', 'status', 'attempts',
        'backend', 'provider_message_id', 'error', 'created_at', 'sent_at',
    )
    date_hierarchy = 'created_at'
    actions = ('resend',)

    def has_add_permission(self, request):
        return False

    @admin.action(description='ارسال مجدد پیام‌های انتخاب‌شده')
    def resend(self, request, queryset):
        from .tasks import deliver_notification

        count = 0
        for notification in queryset.exclude(status=Notification.STATUS_SENT):
            deliver_notification.delay(notification.id)
            count += 1
        self.message_user(request, f'{count} پیام دوباره به صف ارسال رفت.', messages.SUCCESS)
