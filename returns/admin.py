"""
پنل ادمین مرجوعی کالا (Phase 1 - Part B.2).

هر تغییر وضعیت/مبلغ روی ReturnRequest/ReturnItem فقط از طریق اکشن‌های زیر (که مستقیماً
returns/services.py را صدا می‌زنند) انجام می‌شود؛ به همین دلیل تقریباً همه‌ی فیلدهای
ReturnRequestAdmin فقط‌خواندنی‌اند - فرم تغییر مستقیم اصلاً وجود ندارد که چیزی را دور بزند.
دقیقاً هم‌الگوی accounts.admin.CustomUserAdmin.approve_selected/reject_selected (اکشن +
صفحه‌ی میانی برای ورودی اضافه) و wallet.admin.WithdrawalRequestAdmin (فیلدهای مالی/اسنپ‌شات
فقط‌خواندنی).
"""

from django import forms
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

from . import services
from .models import ReturnAttachment, ReturnabilityRule, ReturnItem, ReturnReason, ReturnRequest


class RejectReturnRequestForm(forms.Form):
    reason = forms.CharField(label='دلیل رد', widget=forms.Textarea(attrs={'rows': 3}), required=True)


class MarkItemsReceivedForm(forms.Form):
    """ به‌ازای هر ReturnItem یک فیلد عددی داینامیک؛ سقف هرکدام همان requested_quantity خودش است """

    def __init__(self, *args, items, **kwargs):
        super().__init__(*args, **kwargs)
        for item in items:
            self.fields[f'item_{item.pk}'] = forms.IntegerField(
                label=f'{item.order_item} (درخواستی: {item.requested_quantity})',
                min_value=0, max_value=item.requested_quantity, initial=item.requested_quantity,
            )


@admin.register(ReturnReason)
class ReturnReasonAdmin(admin.ModelAdmin):
    list_display = ('title', 'shipping_cost_payer', 'requires_description', 'is_active', 'order')
    list_filter = ('is_active', 'shipping_cost_payer', 'requires_description')
    list_editable = ('order', 'is_active')
    search_fields = ('title',)
    ordering = ('order', 'id')


@admin.register(ReturnabilityRule)
class ReturnabilityRuleAdmin(admin.ModelAdmin):
    list_display = ('reason_text', 'scope', 'is_active')
    list_filter = ('scope', 'is_active')
    search_fields = ('reason_text',)
    filter_horizontal = ('products', 'categories')


class ReturnItemInline(admin.TabularInline):
    """
    approved_quantity عمداً فقط‌خواندنی است: تنها مسیرِ نوشتنش اکشنِ «ثبت دریافت فیزیکی کالا»ست
    (returns/services.py:mark_items_received) که هم‌زمان وضعیت درخواست را هم به ITEM_RECEIVED
    می‌برد؛ اگر این‌جا مستقیم قابل‌ویرایش بود، می‌شد approved_quantity را بدون تغییر وضعیت نوشت -
    دقیقاً همان چیزی که معماری این اپ می‌خواهد جلویش را بگیرد.
    """
    model = ReturnItem
    extra = 0
    can_delete = False
    fields = ('order_item', 'reason', 'requested_quantity', 'approved_quantity', 'refund_amount', 'attachments_preview')
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description='مدارک پیوست')
    def attachments_preview(self, obj):
        if not obj.pk:
            return '—'
        attachments = list(obj.attachments.all())
        if not attachments:
            return 'بدون مدرک'
        return format_html_join(
            mark_safe('<br>'), '<a href="{}" target="_blank" rel="noopener">{}</a>',
            ((a.file.url, a.original_filename or f'مدرک #{a.pk}') for a in attachments),
        )


class ReturnAttachmentInline(admin.TabularInline):
    model = ReturnAttachment
    extra = 0
    fields = ('file', 'attachment_type', 'original_filename', 'created_at')
    readonly_fields = ('created_at',)


@admin.register(ReturnItem)
class ReturnItemAdmin(admin.ModelAdmin):
    """
    ثبت مستقل فقط برای دیدن/مدیریتِ مدارک پیوستی هر قلم (ReturnAttachmentInline)؛ Django
    اینلاین تودرتو (ReturnAttachment زیرِ ReturnItem زیرِ ReturnRequest) را بدون افزودن یک
    پکیج جدا پشتیبانی نمی‌کند، پس مدارک این‌جا (و به‌صورت پیش‌نمایش لینکی در ReturnItemInline
    بالا) در دسترس‌اند.
    """
    list_display = ('id', 'return_request', 'order_item', 'requested_quantity', 'approved_quantity', 'refund_amount')
    list_filter = ('return_request__status',)
    search_fields = ('return_request__id', 'order_item__order__id')
    readonly_fields = ('return_request', 'order_item', 'reason', 'requested_quantity', 'approved_quantity', 'refund_amount', 'description')
    inlines = (ReturnAttachmentInline,)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ReturnRequest)
class ReturnRequestAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'order_link', 'user', 'status', 'refund_method', 'requested_at', 'total_refund_amount_display',
    )
    list_filter = ('status', 'refund_method', 'requested_at')
    search_fields = ('id', 'order__id', 'user__phone_number', 'user__first_name', 'user__last_name')
    ordering = ('-requested_at',)
    inlines = (ReturnItemInline,)
    actions = (
        'approve_selected_action', 'mark_items_received_action', 'mark_refund_pending_action',
        'reject_selected_action', 'complete_refund_action',
    )

    # فقط یادداشتِ حسابداری دستی قابل‌ویرایش است؛ بقیه فقط از طریق اکشن‌های پایین تغییر می‌کنند
    readonly_fields = (
        'order', 'user', 'status', 'refund_method', 'bank_account', 'bank_account_holder_snapshot',
        'bank_card_snapshot', 'bank_iban_snapshot', 'wallet_transaction', 'shipping_refunded',
        'shipping_refund_amount', 'rejection_reason', 'requested_at', 'decided_at', 'decided_by',
        'item_received_at', 'received_by', 'completed_at', 'completed_by', 'total_refund_amount_display',
    )

    def has_add_permission(self, request):
        return False   # فقط از returns/services.py:create_return_request (با اعتبارسنجی کامل) ساخته می‌شود

    def has_delete_permission(self, request, obj=None):
        return False    # سابقه‌ی مالی/درخواست حذف نمی‌شود - دقیقاً هم‌دلیل wallet.WalletTransaction

    @admin.display(description='سفارش')
    def order_link(self, obj):
        url = reverse('admin:orders_order_change', args=[obj.order_id])
        return format_html('<a href="{}">#{}</a>', url, obj.order_id)

    @admin.display(description='مبلغ کل بازپرداخت')
    def total_refund_amount_display(self, obj):
        return f'{obj.total_refund_amount:,.0f} تومان'

    # ------------------------------------------------------------------ اکشن‌ها

    @admin.action(description='تأیید اولیه‌ی درخواست‌های انتخاب‌شده (PENDING → APPROVED)')
    def approve_selected_action(self, request, queryset):
        ok, errors = 0, []
        for obj in queryset:
            try:
                services.approve_return_request(obj, request.user)
                ok += 1
            except services.InvalidReturnStateError as exc:
                errors.append(f'#{obj.pk}: {exc}')
        self._report(request, ok, errors, 'تأیید شد')

    @admin.action(description='ارجاع به صف بازپرداخت (ITEM_RECEIVED → REFUND_PENDING)')
    def mark_refund_pending_action(self, request, queryset):
        ok, errors = 0, []
        for obj in queryset:
            try:
                services.mark_refund_pending(obj)
                ok += 1
            except services.InvalidReturnStateError as exc:
                errors.append(f'#{obj.pk}: {exc}')
        self._report(request, ok, errors, 'به صف بازپرداخت ارجاع شد')

    @admin.action(description='تکمیل واریز وجه (REFUND_PENDING → COMPLETED)')
    def complete_refund_action(self, request, queryset):
        ok, errors = 0, []
        for obj in queryset:
            try:
                services.complete_refund(obj, request.user)
                ok += 1
            except (services.InvalidReturnStateError, ValueError) as exc:
                errors.append(f'#{obj.pk}: {exc}')
        self._report(request, ok, errors, 'تکمیل شد')

    @admin.action(description='ثبت دریافت فیزیکی کالا و تعداد تأییدشده (APPROVED → ITEM_RECEIVED)')
    def mark_items_received_action(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, 'برای این عملیات دقیقاً یک درخواست را انتخاب کنید.', messages.ERROR)
            return None
        target = queryset.get()
        items = list(target.items.select_related('order_item__product'))

        if 'apply' in request.POST:
            form = MarkItemsReceivedForm(request.POST, items=items)
            if form.is_valid():
                approved_quantities = {item.pk: form.cleaned_data[f'item_{item.pk}'] for item in items}
                try:
                    services.mark_items_received(target, approved_quantities, request.user)
                except (services.InvalidReturnStateError, ValueError) as exc:
                    self.message_user(request, str(exc), messages.ERROR)
                else:
                    self.message_user(request, f'دریافت کالای درخواست #{target.pk} ثبت شد.', messages.SUCCESS)
                return redirect(reverse('admin:returns_returnrequest_changelist'))
        else:
            form = MarkItemsReceivedForm(items=items)

        return TemplateResponse(request, 'returns/admin/mark_items_received.html', {
            **self.admin_site.each_context(request), 'opts': self.model._meta, 'form': form,
            'target': target, 'action_checkbox_name': helpers.ACTION_CHECKBOX_NAME,
            'action_name': 'mark_items_received_action', 'title': f'ثبت دریافت فیزیکی کالا - درخواست #{target.pk}',
            'submit_label': 'ثبت تعداد تأییدشده',
        })

    @admin.action(description='رد درخواست انتخاب‌شده (با دلیل)')
    def reject_selected_action(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, 'برای رد کردن دقیقاً یک درخواست را انتخاب کنید.', messages.ERROR)
            return None
        target = queryset.get()

        if 'apply' in request.POST:
            form = RejectReturnRequestForm(request.POST)
            if form.is_valid():
                try:
                    services.reject_return_request(target, form.cleaned_data['reason'], request.user)
                except services.InvalidReturnStateError as exc:
                    self.message_user(request, str(exc), messages.ERROR)
                else:
                    self.message_user(request, f'درخواست #{target.pk} رد شد.', messages.SUCCESS)
                return redirect(reverse('admin:returns_returnrequest_changelist'))
        else:
            form = RejectReturnRequestForm()

        return TemplateResponse(request, 'returns/admin/reject_return_request.html', {
            **self.admin_site.each_context(request), 'opts': self.model._meta, 'form': form,
            'target': target, 'action_checkbox_name': helpers.ACTION_CHECKBOX_NAME,
            'action_name': 'reject_selected_action', 'title': f'رد درخواست مرجوعی #{target.pk}',
            'submit_label': 'رد درخواست',
        })

    def _report(self, request, ok, errors, verb):
        if ok:
            self.message_user(request, f'{ok} درخواست {verb}.', messages.SUCCESS)
        if errors:
            self.message_user(request, 'ناموفق: ' + ' | '.join(errors), messages.ERROR)
