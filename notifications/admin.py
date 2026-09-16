from django.contrib import admin, messages

from .models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'template_key', 'recipient', 'status', 'attempts', 'sent_at')
    list_filter = ('status', 'template_key', 'created_at')
    search_fields = ('recipient', 'text', 'provider_message_id')
    readonly_fields = (
        'recipient', 'template_key', 'text', 'status', 'attempts',
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
