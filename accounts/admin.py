from django import forms
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.core.exceptions import ValidationError
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.html import format_html
from django.utils import timezone
from .models import Address, ApprovalStatus, CustomUser, OTPRequest


class ApproveUserForm(forms.Form):
    price_level = forms.TypedChoiceField(
        choices=CustomUser.PRICE_LEVELS, coerce=int, label='سطح قیمت',
        help_text='بر اساس همین انتخاب، کاربر از حالا قیمت‌های سطح مربوطه را می‌بیند و می‌تواند سفارش ثبت کند.',
    )


class RejectUserForm(forms.Form):
    reason = forms.CharField(
        label='دلیل رد', widget=forms.Textarea(attrs={'rows': 3}), required=False,
        help_text='به کاربر نمایش داده می‌شود؛ اختیاری است.',
    )


class AddressInlineFormSet(forms.BaseInlineFormSet):
    def clean(self):
        super().clean()
        defaults = [f for f in self.forms
                    if f.cleaned_data and not f.cleaned_data.get('DELETE') and f.cleaned_data.get('is_default')]
        if len(defaults) > 1:
            raise forms.ValidationError('فقط یک آدرس می‌تواند پیش‌فرض باشد.')


class AddressInline(admin.TabularInline):
    model = Address
    formset = AddressInlineFormSet
    extra = 0
    fields = ('title', 'receiver_first_name', 'receiver_last_name', 'receiver_phone',
              'city', 'zone', 'postal_code', 'address', 'is_default')
    autocomplete_fields = ('city', 'zone')


@admin.register(Address)
class AddressAdmin(admin.ModelAdmin):
    list_display = ('title', 'user', 'receiver_full_name', 'city', 'zone', 'is_default', 'created_at')
    list_filter = ('is_default', 'city__province')
    list_select_related = ('user', 'city', 'city__province', 'zone')
    search_fields = ('title', 'user__phone_number', 'receiver_first_name', 'receiver_last_name', 'postal_code')
    autocomplete_fields = ('user', 'city', 'zone')
    actions = ('make_default',)

    @admin.action(description='تنظیم به عنوان آدرس پیش‌فرض کاربر (فقط یک آدرس انتخاب شود)')
    def make_default(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, 'برای این عملیات دقیقاً یک آدرس انتخاب کنید.', messages.ERROR)
            return
        queryset.get().set_default()
        self.message_user(request, 'آدرس پیش‌فرض کاربر تغییر کرد.', messages.SUCCESS)


@admin.register(CustomUser)
class CustomUserAdmin(admin.ModelAdmin):
    inlines = (AddressInline,)
    # اضافه شدن نام و نام خانوادگی به لیست اصلی
    list_display = ('phone_number', 'get_full_name', 'colored_status', 'colored_approval_status', 'erp_code', 'date_joined', 'is_active')

    list_filter = ('status', 'approval_status', 'is_active', 'is_staff', 'date_joined')

    # اضافه شدن کد ملی و نام به باکس جستجو
    search_fields = ('phone_number', 'erp_code', 'national_code', 'first_name', 'last_name', 'business_name')

    ordering = ('-date_joined',)

    # approval_status/approved_at/approved_by عمداً readonly: تنها راه مجاز تغییرشان اکشن‌های
    # approve_selected/reject_selected (که از متد مدل CustomUser.approve()/reject() عبور
    # می‌کنند) است، نه دراپ‌داون دستی در همین فرم — طبق تصمیم معماریِ تأییدشده.
    readonly_fields = ('date_joined', 'last_login', 'retry_count', 'last_sync_error',
                       'approval_status', 'approved_at', 'approved_by', 'rejected_by')

    actions = ('approve_selected', 'reject_selected')

    # دسته‌بندی جدید و بسیار مرتب فیلدها در صفحه ویرایش
    fieldsets = (
        ('اطلاعات ورود و پایه', {
            'fields': ('phone_number', 'status', 'erp_code')
        }),
        ('اطلاعات هویتی', {
            'fields': ('first_name', 'last_name', 'national_code', 'business_name')
        }),
        ('تأیید تجاری (مستقل از وضعیت هلوی بالا؛ فقط با اکشن‌های تأیید/رد تغییر می‌کند)', {
            'fields': ('approval_status', 'price_level', 'approved_at', 'approved_by',
                      'rejected_by', 'rejection_reason'),
        }),
        ('وضعیت یکپارچه‌سازی هلو', {
            'fields': ('retry_count', 'last_sync_error')
        }),
        ('دسترسی‌ها و تاریخ‌ها', {
            'fields': ('is_active', 'is_staff', 'is_superuser', 'date_joined', 'last_login')
        }),
    )

    # آپدیت شدن رنگ‌ها بر اساس ماشین وضعیت جدید
    def colored_status(self, obj):
        colors = {
            'PENDING_PROFILE': '#ff9800',  # نارنجی - نیازمند تکمیل
            'PENDING_ERP_SYNC': '#2196f3', # آبی - در انتظار هلو
            'ACTIVE': '#4caf50',           # سبز - فعال
            'REJECTED': '#f44336',         # قرمز - مسدود
        }
        color = colors.get(obj.status, '#000000')
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 10px; border-radius: 12px; font-weight: bold; font-size: 11px;">{}</span>',
            color,
            obj.get_status_display()
        )
    colored_status.short_description = 'وضعیت کاربر (هلو)'

    def colored_approval_status(self, obj):
        colors = {
            ApprovalStatus.PENDING: '#ff9800',   # نارنجی - در انتظار بررسی
            ApprovalStatus.APPROVED: '#4caf50',  # سبز - تأیید شده
            ApprovalStatus.REJECTED: '#f44336',  # قرمز - رد شده
        }
        color = colors.get(obj.approval_status, '#000000')
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 10px; border-radius: 12px; font-weight: bold; font-size: 11px;">{}</span>',
            color,
            obj.get_approval_status_display()
        )
    colored_approval_status.short_description = 'تأیید تجاری'

    def get_full_name(self, obj):
        name = f"{obj.first_name or ''} {obj.last_name or ''}".strip()
        return name if name else "-"
    get_full_name.short_description = 'نام و نام خانوادگی'

    # ----- تأیید/رد (اکشن + صفحه‌ی میانی، دقیقاً الگوی promotions.admin.CouponAdmin.bulk_generate) -----

    @admin.action(description='تأیید کاربر و تعیین سطح قیمت')
    def approve_selected(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, 'برای تأیید دقیقاً یک کاربر را انتخاب کنید.', messages.ERROR)
            return None
        target = queryset.get()
        if not target.is_profile_complete():
            self.message_user(
                request, f'کاربر {target} هنوز پروفایلش (نام/نام‌خانوادگی/کد ملی) را تکمیل نکرده؛ قابل تأیید نیست.',
                messages.ERROR,
            )
            return None
        if 'apply' in request.POST:
            form = ApproveUserForm(request.POST)
            if form.is_valid():
                try:
                    _, changed = target.approve(price_level=form.cleaned_data['price_level'], approved_by=request.user)
                except ValidationError as e:
                    self.message_user(request, '؛ '.join(e.messages), messages.ERROR)
                else:
                    msg = f'کاربر {target} تأیید شد.' if changed else f'کاربر {target} از قبل تأیید شده بود.'
                    self.message_user(request, msg, messages.SUCCESS if changed else messages.WARNING)
                return redirect(reverse('admin:accounts_customuser_changelist'))
        else:
            form = ApproveUserForm(initial={'price_level': target.price_level})
        return TemplateResponse(request, 'accounts/admin/approval_action.html', {
            **self.admin_site.each_context(request), 'opts': self.model._meta, 'form': form, 'target_user': target,
            'action_checkbox_name': helpers.ACTION_CHECKBOX_NAME, 'action_name': 'approve_selected',
            'title': 'تأیید کاربر', 'submit_label': 'تأیید و تعیین سطح قیمت',
        })

    @admin.action(description='رد کردن کاربر')
    def reject_selected(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, 'برای رد کردن دقیقاً یک کاربر را انتخاب کنید.', messages.ERROR)
            return None
        target = queryset.get()
        if 'apply' in request.POST:
            form = RejectUserForm(request.POST)
            if form.is_valid():
                _, changed = target.reject(reason=form.cleaned_data['reason'], rejected_by=request.user)
                msg = f'کاربر {target} رد شد.' if changed else f'کاربر {target} از قبل رد شده بود.'
                self.message_user(request, msg, messages.SUCCESS if changed else messages.WARNING)
                return redirect(reverse('admin:accounts_customuser_changelist'))
        else:
            form = RejectUserForm()
        return TemplateResponse(request, 'accounts/admin/approval_action.html', {
            **self.admin_site.each_context(request), 'opts': self.model._meta, 'form': form, 'target_user': target,
            'action_checkbox_name': helpers.ACTION_CHECKBOX_NAME, 'action_name': 'reject_selected',
            'title': 'رد کردن کاربر', 'submit_label': 'رد کردن',
        })


@admin.register(OTPRequest)
class OTPRequestAdmin(admin.ModelAdmin):
    # کدهای بخش OTP کاملاً درست و اصولی بودند، تغییری نیاز ندارند
    list_display = ('phone_number', 'code', 'purpose', 'attempt_count', 'is_expired', 'used_status')
    list_filter = ('purpose', 'created_at')
    search_fields = ('phone_number', 'code')
    ordering = ('-created_at',)
    readonly_fields = ('phone_number', 'code', 'purpose', 'ip_address', 'attempt_count', 'created_at', 'expires_at', 'used_at')

    def is_expired(self, obj):
        expired = timezone.now() > obj.expires_at
        if expired and not obj.used_at:
            return format_html('<span style="color: #f44336;">منقضی شده</span>')
        elif obj.used_at:
            return format_html('<span style="color: #9e9e9e;">-</span>')
        return format_html('<span style="color: #4caf50;">معتبر</span>')
    is_expired.short_description = 'وضعیت زمان'

    def used_status(self, obj):
        if obj.used_at:
            return format_html('<span style="color: #4caf50;">استفاده شده در {}</span>', obj.used_at.strftime('%H:%M'))
        return format_html('<span style="color: #ff9800;">استفاده نشده</span>')
    used_status.short_description = 'وضعیت مصرف'