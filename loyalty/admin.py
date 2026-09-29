"""
پنل ادمین باشگاه مشتریان (Phase 1: فقط دفترکل و عملیات دستی). اعطا/کسر دستی هرگز مستقیم
current_balance/LoyaltyTransaction را دستکاری نمی‌کند - همیشه از loyalty/services.py
(credit_points/debit_points) رد می‌شود که خودش select_for_update + بررسی موجودی کافی را
تضمین می‌کند؛ دقیقاً همان الگوی wallet/admin.py::WithdrawalRequestAdmin (اکشن به‌ازای هر ردیف +
صفحه‌ی میانی برای دریافت مقدار/علت).
"""

import uuid

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from . import reports, services
from .exceptions import IdempotencyKeyConflictError, InsufficientPointsError
from .models import LoyaltyAccount, LoyaltyReward, LoyaltyTier, LoyaltyTierHistory, LoyaltyTransaction


class ManualAdjustmentForm(forms.Form):
    """
    adjustment_token: یک توکن یک‌بارمصرف که فقط در رندر اول (GET، حالت unbound) تازه تولید
    می‌شود؛ بعد از آن با خودِ فرم (پنهان) رفت‌وبرگشت می‌کند و عیناً به credit_points/debit_points
    به‌عنوان idempotency_key پاس داده می‌شود - محافظت در برابر دابل‌کلیک/دابل‌ساب‌میت ادمین: دو
    درخواست هم‌زمان با همان توکن، دومی فقط رکورد اولی را برمی‌گرداند، نه یک تراکنش تازه.
    """
    amount = forms.IntegerField(label='تعداد امتیاز', min_value=1)
    reason = forms.CharField(label='علت (اجباری)', widget=forms.Textarea(attrs={'rows': 3}))
    adjustment_token = forms.CharField(widget=forms.HiddenInput(), required=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.fields['adjustment_token'].initial = uuid.uuid4().hex


class LoyaltyTransactionInline(admin.TabularInline):
    """ تاریخچه‌ی دفترکل همین حساب - کاملاً فقط‌خواندنی؛ افزودن/حذف فقط از loyalty/services.py. """
    model = LoyaltyTransaction
    extra = 0
    can_delete = False
    fields = (
        'created_at', 'transaction_type', 'amount', 'balance_after', 'reason',
        'source_type', 'source_id', 'created_by',
    )
    readonly_fields = fields
    ordering = ('-created_at',)

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(LoyaltyAccount)
class LoyaltyAccountAdmin(admin.ModelAdmin):
    list_display = (
        'id', 'user_display', 'current_balance', 'lifetime_earned', 'lifetime_redeemed',
        'updated_at', 'action_buttons',
    )
    search_fields = ('user__phone_number', 'user__first_name', 'user__last_name')
    readonly_fields = ('user', 'current_balance', 'lifetime_earned', 'lifetime_redeemed', 'created_at', 'updated_at')
    inlines = (LoyaltyTransactionInline,)
    ordering = ('-updated_at',)
    change_list_template = 'admin/loyalty/loyaltyaccount/change_list.html'

    def has_add_permission(self, request):
        return False   # فقط از loyalty.services (credit_points/debit_points -> get_or_create_for_user) ساخته می‌شود

    def has_delete_permission(self, request, obj=None):
        return False    # سابقه‌ی مالی حذف نمی‌شود - دقیقاً هم‌دلیل wallet.Wallet

    @admin.display(description='کاربر')
    def user_display(self, obj):
        return obj.user.phone_number

    @admin.display(description='عملیات دستی')
    def action_buttons(self, obj):
        credit_url = reverse('admin:loyalty_loyaltyaccount_credit', args=[obj.pk])
        debit_url = reverse('admin:loyalty_loyaltyaccount_debit', args=[obj.pk])
        return format_html(
            '<a class="button" href="{}">اعطای امتیاز</a>&nbsp;'
            '<a class="button" style="background:#ba2121" href="{}">کسر امتیاز</a>',
            credit_url, debit_url,
        )

    def get_urls(self):
        custom = [
            path('<int:pk>/credit/', self.admin_site.admin_view(self.credit_view), name='loyalty_loyaltyaccount_credit'),
            path('<int:pk>/debit/', self.admin_site.admin_view(self.debit_view), name='loyalty_loyaltyaccount_debit'),
            path('report/', self.admin_site.admin_view(self.report_view), name='loyalty_loyaltyaccount_report'),
        ]
        return custom + super().get_urls()

    def report_view(self, request):
        """ گزارش مالی و حسابرسی تعهدات لجر (Phase 5D-2) با کوئری‌های تجمیعیِ ثابت؛ فقط برای دارندگان مجوز مشاهده """
        if not self.has_view_permission(request):
            raise PermissionDenied
        context = {
            **self.admin_site.each_context(request), 'opts': self.model._meta, 'title': 'گزارش مالی باشگاه مشتریان',
            'report': reports.build_financial_report(),
        }
        return TemplateResponse(request, 'admin/loyalty/loyaltyaccount/report.html', context)

    def credit_view(self, request, pk):
        obj = self.get_object(request, str(pk))
        if obj is None:
            messages.error(request, 'حساب پیدا نشد.')
            return redirect(reverse('admin:loyalty_loyaltyaccount_changelist'))

        if request.method == 'POST':
            form = ManualAdjustmentForm(request.POST)
            if form.is_valid():
                token = form.cleaned_data['adjustment_token']
                try:
                    services.credit_points(
                        obj.user, form.cleaned_data['amount'], LoyaltyTransaction.ADMIN_CREDIT,
                        form.cleaned_data['reason'], created_by=request.user,
                        idempotency_key=f'admin-credit-{obj.pk}-{token}',
                    )
                except IdempotencyKeyConflictError:
                    messages.error(request, 'این فرم قبلاً با مقدار دیگری ارسال شده؛ صفحه را دوباره باز کنید.')
                else:
                    messages.success(request, f'{form.cleaned_data["amount"]} امتیاز به حساب #{obj.pk} اعطا شد.')
                    return redirect(reverse('admin:loyalty_loyaltyaccount_changelist'))
        else:
            form = ManualAdjustmentForm()

        return render(request, 'admin/loyalty/loyaltyaccount_adjust.html', {
            **self.admin_site.each_context(request),
            'form': form, 'object': obj, 'opts': self.model._meta,
            'title': f'اعطای دستی امتیاز - حساب #{obj.pk}', 'submit_label': 'اعطای امتیاز',
        })

    def debit_view(self, request, pk):
        obj = self.get_object(request, str(pk))
        if obj is None:
            messages.error(request, 'حساب پیدا نشد.')
            return redirect(reverse('admin:loyalty_loyaltyaccount_changelist'))

        if request.method == 'POST':
            form = ManualAdjustmentForm(request.POST)
            if form.is_valid():
                token = form.cleaned_data['adjustment_token']
                try:
                    services.debit_points(
                        obj.user, form.cleaned_data['amount'], LoyaltyTransaction.ADMIN_DEBIT,
                        form.cleaned_data['reason'], created_by=request.user,
                        idempotency_key=f'admin-debit-{obj.pk}-{token}',
                    )
                except InsufficientPointsError as exc:
                    messages.error(request, str(exc))
                except IdempotencyKeyConflictError:
                    messages.error(request, 'این فرم قبلاً با مقدار دیگری ارسال شده؛ صفحه را دوباره باز کنید.')
                else:
                    messages.success(request, f'{form.cleaned_data["amount"]} امتیاز از حساب #{obj.pk} کسر شد.')
                    return redirect(reverse('admin:loyalty_loyaltyaccount_changelist'))
        else:
            form = ManualAdjustmentForm()

        return render(request, 'admin/loyalty/loyaltyaccount_adjust.html', {
            **self.admin_site.each_context(request),
            'form': form, 'object': obj, 'opts': self.model._meta,
            'title': f'کسر دستی امتیاز - حساب #{obj.pk}', 'submit_label': 'کسر امتیاز',
        })


@admin.register(LoyaltyTransaction)
class LoyaltyTransactionAdmin(admin.ModelAdmin):
    """ کاملاً فقط‌خواندنی - رکوردهای دفترکل فقط از loyalty/services.py ساخته می‌شوند. """
    list_display = ('id', 'account', 'transaction_type', 'amount', 'balance_after', 'reason', 'created_at')
    list_filter = ('transaction_type', 'created_at', 'source_type')
    search_fields = ('account__user__phone_number', 'reason', 'idempotency_key')
    ordering = ('-created_at',)
    readonly_fields = (
        'account', 'amount', 'balance_after', 'transaction_type', 'source_type', 'source_id',
        'idempotency_key', 'expires_at', 'remaining_amount', 'reason', 'created_by', 'created_at',
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(LoyaltyTier)
class LoyaltyTierAdmin(admin.ModelAdmin):
    """
    فهرست/ویرایش سطوح داینامیک (Phase 3C). list_editable شامل rank/threshold/is_active است تا
    ادمین بتواند ترتیب و آستانه را مستقیم از صفحه‌ی فهرست تغییر دهد - دقیقاً هم‌الگوی
    returns.admin.ReturnReasonAdmin (list_editable روی order/is_active). اعتبارسنجی صعودی
    (LoyaltyTier.clean()) چه از فرم افزودن/ویرایش، چه از ویرایش سریعِ فهرست، هر دو از مسیر
    استاندارد ModelForm._post_clean اجرا می‌شود - نیازی به کد اضافه در این ادمین نیست. همین
    قاعده برای اعتبارسنجی کراس‌واک (legacy_equivalent_index، فاز ۵B-1) هم صادق است.
    """
    list_display = ('rank', 'title', 'threshold', 'legacy_equivalent_index', 'is_active', 'badge_color')
    list_display_links = ('title',)   # چون rank داخل list_editable است، نمی‌تواند اولین ستون/لینک هم باشد
    list_editable = ('rank', 'threshold', 'is_active')
    fields = ('title', 'rank', 'threshold', 'legacy_equivalent_index', 'is_active', 'badge_color')
    ordering = ('rank',)
    search_fields = ('title',)

    def has_delete_permission(self, request, obj=None):
        return False   # فقط غیرفعال‌سازی (is_active=False) مسیر پشتیبانی‌شده است - نگاه کنید LoyaltyTier.delete()


@admin.register(LoyaltyReward)
class LoyaltyRewardAdmin(admin.ModelAdmin):
    """ کاتالوگ پاداش‌های قابل‌بازخرید با امتیاز (Phase 4B). خودِ بازخرید/تخصیص کوپن فقط از
    loyalty/reward_redemption.py انجام می‌شود - این ادمین صرفاً کاتالوگ (تعریف پاداش) را مدیریت می‌کند. """
    list_display = ('display_order', 'title', 'points_cost', 'coupon', 'is_active')
    list_display_links = ('title',)
    list_editable = ('display_order', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('title', 'coupon__code', 'coupon__title')
    autocomplete_fields = ('coupon',)
    ordering = ('display_order', 'id')


@admin.register(LoyaltyTierHistory)
class LoyaltyTierHistoryAdmin(admin.ModelAdmin):
    """ دفترکل رویدادهای ارتقای رتبه (Phase 5C-2) - کاملاً فقط‌خواندنی؛ فقط توسط
    loyalty/progression.py::credit_points_with_progression ساخته می‌شود. """
    list_display = ('created_at', 'account', 'old_tier', 'new_tier', 'notified_at')
    list_filter = ('new_tier',)
    search_fields = ('account__user__phone_number',)
    readonly_fields = ('account', 'old_tier', 'new_tier', 'triggering_transaction', 'created_at', 'notified_at')
    ordering = ('-created_at',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
