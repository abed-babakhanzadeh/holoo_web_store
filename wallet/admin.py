"""
پنل ادمین کیف پول. تأیید/پرداخت/رد هرگز مستقیم status/balance را دستکاری نمی‌کند؛ همیشه از
wallet/services.py (approve_withdrawal/mark_withdrawal_paid/reject_withdrawal) رد می‌شود که
خودش select_for_update + چک وضعیت مجاز را تضمین می‌کند - یعنی جلوگیری ساختاری از پردازش
دوباره‌ی یک درخواست از قبل تصمیم‌گیری‌شده در همان لایه‌ی سرویس است، نه فقط در UI ادمین.

Wallet/WalletTopupRequest فقط‌خواندنی ثبت شده‌اند (نه برای ویرایش، صرفاً برای دیده‌شدنِ بهتر
بخش «کیف پول» در سایدبار ادمین - قبلاً فقط یک مدل در این بخش بود).
"""

from django import forms
from django.contrib import admin, messages
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils.html import format_html

from . import services
from .models import Wallet, WalletTopupRequest, WithdrawalRequest


class RejectionReasonForm(forms.Form):
    reason = forms.CharField(label='دلیل رد', widget=forms.Textarea(attrs={'rows': 3}), required=True)


@admin.register(WithdrawalRequest)
class WithdrawalRequestAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'user_phone', 'amount', 'card_number_snapshot', 'iban_snapshot',
        'account_holder_snapshot', 'status', 'requested_at', 'action_buttons',
    )
    list_filter = ('status',)
    ordering = ('-requested_at',)
    readonly_fields = (
        'wallet', 'amount', 'card_number_snapshot', 'iban_snapshot', 'account_holder_snapshot',
        'status', 'rejection_reason', 'transaction', 'requested_at', 'decided_at', 'decided_by', 'paid_at',
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description='کاربر')
    def user_phone(self, obj):
        return obj.wallet.user.phone_number if obj.wallet.user_id else '-'

    @admin.display(description='اقدام')
    def action_buttons(self, obj):
        if obj.status == WithdrawalRequest.STATUS_PENDING:
            approve_url = reverse('admin:wallet_withdrawalrequest_approve', args=[obj.pk])
            reject_url = reverse('admin:wallet_withdrawalrequest_reject', args=[obj.pk])
            return format_html(
                '<a class="button" href="{}">تأیید درخواست</a>&nbsp;'
                '<a class="button" style="background:#ba2121" href="{}">رد</a>',
                approve_url, reject_url,
            )
        if obj.status == WithdrawalRequest.STATUS_APPROVED:
            pay_url = reverse('admin:wallet_withdrawalrequest_mark_paid', args=[obj.pk])
            reject_url = reverse('admin:wallet_withdrawalrequest_reject', args=[obj.pk])
            return format_html(
                '<a class="button" href="{}">پرداخت شد / تسویه نهایی</a>&nbsp;'
                '<a class="button" style="background:#ba2121" href="{}">رد</a>',
                pay_url, reject_url,
            )
        return '—'

    def get_urls(self):
        custom = [
            path('<int:pk>/approve/', self.admin_site.admin_view(self.approve_view),
                 name='wallet_withdrawalrequest_approve'),
            path('<int:pk>/mark-paid/', self.admin_site.admin_view(self.mark_paid_view),
                 name='wallet_withdrawalrequest_mark_paid'),
            path('<int:pk>/reject/', self.admin_site.admin_view(self.reject_view),
                 name='wallet_withdrawalrequest_reject'),
        ]
        return custom + super().get_urls()

    def approve_view(self, request, pk):
        obj = self.get_object(request, str(pk))
        if obj is None:
            messages.error(request, 'درخواست پیدا نشد.')
        else:
            try:
                services.approve_withdrawal(obj, admin_user=request.user)
                messages.success(request, f'درخواست برداشت #{obj.pk} تأیید شد و در صف واریز دستی قرار گرفت.')
            except services.InvalidWithdrawalStateError as exc:
                messages.error(request, str(exc))
        return redirect(reverse('admin:wallet_withdrawalrequest_changelist'))

    def mark_paid_view(self, request, pk):
        obj = self.get_object(request, str(pk))
        if obj is None:
            messages.error(request, 'درخواست پیدا نشد.')
        else:
            try:
                services.mark_withdrawal_paid(obj, admin_user=request.user)
                messages.success(request, f'درخواست برداشت #{obj.pk} تسویه شد.')
            except services.InvalidWithdrawalStateError as exc:
                messages.error(request, str(exc))
        return redirect(reverse('admin:wallet_withdrawalrequest_changelist'))

    def reject_view(self, request, pk):
        obj = self.get_object(request, str(pk))
        if obj is None:
            messages.error(request, 'درخواست پیدا نشد.')
            return redirect(reverse('admin:wallet_withdrawalrequest_changelist'))
        if obj.status not in (WithdrawalRequest.STATUS_PENDING, WithdrawalRequest.STATUS_APPROVED):
            messages.error(request, 'این درخواست قبلاً تصمیم‌گیری نهایی شده است.')
            return redirect(reverse('admin:wallet_withdrawalrequest_changelist'))

        if request.method == 'POST':
            form = RejectionReasonForm(request.POST)
            if form.is_valid():
                try:
                    services.reject_withdrawal(obj, form.cleaned_data['reason'], admin_user=request.user)
                    messages.success(request, f'درخواست برداشت #{obj.pk} رد شد.')
                except services.InvalidWithdrawalStateError as exc:
                    messages.error(request, str(exc))
                return redirect(reverse('admin:wallet_withdrawalrequest_changelist'))
        else:
            form = RejectionReasonForm()

        context = {
            **self.admin_site.each_context(request),
            'form': form, 'object': obj, 'title': f'رد درخواست برداشت #{obj.pk}',
            'opts': self.model._meta,
        }
        return render(request, 'admin/wallet/withdrawalrequest_reject.html', context)


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    """ فقط‌خواندنی - تغییر balance/reserved_balance همیشه باید از wallet/services.py رد شود """
    list_display = ('id', 'user', 'balance', 'reserved_balance', 'available_balance_display', 'updated_at')
    search_fields = ('user__phone_number',)
    readonly_fields = ('user', 'balance', 'reserved_balance', 'created_at', 'updated_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description='موجودی قابل‌استفاده')
    def available_balance_display(self, obj):
        return obj.available_balance


@admin.register(WalletTopupRequest)
class WalletTopupRequestAdmin(admin.ModelAdmin):
    """ فقط‌خواندنی - صرفاً برای مشاهده‌ی سوابق شارژ؛ خودِ جریان از wallet/views.py مدیریت می‌شود """
    list_display = ('id', 'user_phone', 'amount', 'status', 'authority', 'created_at')
    list_filter = ('status',)
    search_fields = ('wallet__user__phone_number', 'authority')
    readonly_fields = ('wallet', 'amount', 'authority', 'ref_id', 'status', 'transaction', 'created_at', 'updated_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description='کاربر')
    def user_phone(self, obj):
        return obj.wallet.user.phone_number if obj.wallet.user_id else '-'
