from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.template.response import TemplateResponse
from django.urls import path, reverse

from . import console
from .models import ChatEvent, ChatMessage, Conversation, OperatorPresence, QuickReply


class ChatMessageInline(admin.TabularInline):
    model = ChatMessage
    extra = 0
    can_delete = False
    fields = ('seq', 'sender_type', 'operator', 'body', 'is_internal_note', 'created_at')
    readonly_fields = fields
    ordering = ('seq',)

    def has_add_permission(self, request, obj=None):
        return False


class ChatEventInline(admin.TabularInline):
    model = ChatEvent
    extra = 0
    can_delete = False
    fields = ('type', 'actor_type', 'actor_id', 'from_status', 'to_status', 'meta', 'created_at')
    readonly_fields = fields
    ordering = ('id',)

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    """ فهرست و پیشخوان گفتگو. فقط‌خواندنی: پاسخ و تغییر وضعیت فقط از پیشخوان (و ماتریس وضعیت) انجام می‌شود. """
    change_list_template = 'admin/chat/conversation/change_list.html'
    list_display = ('id', 'display_name_col', 'display_phone_col', 'status', 'channel_origin', 'unread_for_operator', 'assigned_operator',
                    'last_message_at')
    list_filter = ('status', 'channel_origin')
    search_fields = ('guest_name', 'guest_phone', 'user__phone_number', 'user__first_name', 'user__last_name', 'last_message_preview')
    ordering = ('-last_message_at', '-id')
    inlines = (ChatMessageInline, ChatEventInline)

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]

    @admin.display(description='نام')
    def display_name_col(self, obj):
        return obj.display_name

    @admin.display(description='موبایل')
    def display_phone_col(self, obj):
        return obj.display_phone

    # دسترسی: کارشناسان (مجوز operate_chat) فقط می‌بینند؛ حذف فقط superuser
    def has_module_permission(self, request):
        return console.is_operator(request.user)

    def has_view_permission(self, request, obj=None):
        return console.is_operator(request.user)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return bool(request.user.is_active and request.user.is_superuser)

    def get_urls(self):
        view = self.admin_site.admin_view
        custom = [
            path('console/', view(self.console_view), name='chat_conversation_console'),
            path('console/api/inbox/', view(console.inbox_api), name='chat_console_inbox'),
            path('console/api/quick-replies/', view(console.quick_replies_api), name='chat_console_quick_replies'),
            path('console/api/operators/', view(console.operators_api), name='chat_console_operators'),
            path('console/api/c/<int:conversation_id>/', view(console.detail_api), name='chat_console_detail'),
            path('console/api/c/<int:conversation_id>/reply/', view(console.reply_api), name='chat_console_reply'),
            path('console/api/c/<int:conversation_id>/read/', view(console.read_api), name='chat_console_read'),
            path('console/api/c/<int:conversation_id>/action/', view(console.action_api), name='chat_console_action'),
        ]
        return custom + super().get_urls()

    def console_view(self, request):
        if not console.is_operator(request.user):
            raise PermissionDenied
        from products.models import SiteSettings
        cfg = SiteSettings.cached()
        context = {
            **self.admin_site.each_context(request), 'opts': self.model._meta, 'title': 'پیشخوان گفتگو',
            'api': {
                'inbox': reverse('admin:chat_console_inbox'), 'quick_replies': reverse('admin:chat_console_quick_replies'),
                'operators': reverse('admin:chat_console_operators'),
                'detail': reverse('admin:chat_console_detail', args=[0]), 'reply': reverse('admin:chat_console_reply', args=[0]),
                'read': reverse('admin:chat_console_read', args=[0]), 'action': reverse('admin:chat_console_action', args=[0]),
            },
            'poll_inbox_ms': 4000, 'poll_detail_ms': 2500, 'chat_enabled': cfg.chat_enabled,
        }
        return TemplateResponse(request, 'admin/chat/console.html', context)


@admin.register(QuickReply)
class QuickReplyAdmin(admin.ModelAdmin):
    """ پاسخ‌های آماده. کارشناس فقط پاسخ‌های شخصی خودش را می‌سازد/ویرایش می‌کند؛ پاسخ عمومی (بدون مالک) را superuser مدیریت می‌کند. """
    list_display = ('title', 'shortcut', 'owner', 'is_active', 'sort_order', 'usage_count')
    list_filter = ('is_active',)
    search_fields = ('title', 'body', 'shortcut')
    list_editable = ('is_active', 'sort_order')
    fields = ('title', 'shortcut', 'body', 'owner', 'is_active', 'sort_order')

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs if request.user.is_superuser else qs.filter(owner=request.user)

    def get_fields(self, request, obj=None):
        fields = list(super().get_fields(request, obj))
        if not request.user.is_superuser and 'owner' in fields:
            fields.remove('owner')
        return fields

    def save_model(self, request, obj, form, change):
        if not request.user.is_superuser:
            obj.owner = request.user
        super().save_model(request, obj, form, change)

    def has_module_permission(self, request):
        return console.is_operator(request.user)

    def _own(self, request, obj):
        return request.user.is_superuser or obj is None or obj.owner_id == request.user.pk

    def has_view_permission(self, request, obj=None):
        return console.is_operator(request.user) and self._own(request, obj)

    def has_add_permission(self, request):
        return console.is_operator(request.user)

    def has_change_permission(self, request, obj=None):
        return console.is_operator(request.user) and self._own(request, obj)

    def has_delete_permission(self, request, obj=None):
        return console.is_operator(request.user) and self._own(request, obj)


@admin.register(OperatorPresence)
class OperatorPresenceAdmin(admin.ModelAdmin):
    list_display = ('operator', 'is_online', 'last_seen')
    readonly_fields = ('operator', 'is_online', 'last_seen')

    def has_module_permission(self, request):
        return bool(request.user.is_active and request.user.is_superuser)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
