import csv

from django import forms
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join
from django.utils import timezone
from . import cheque_credit_service as credit_service
from .models import (Address, ApprovalStatus, ChequeCreditDocument, ChequeCreditRequest, CustomUser, OTPRequest,
                     UserBankAccount)


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


class UserBankAccountInline(admin.TabularInline):
    model = UserBankAccount
    extra = 0
    fields = ('account_holder_first_name', 'account_holder_last_name', 'card_number', 'iban', 'is_default')


@admin.register(UserBankAccount)
class UserBankAccountAdmin(admin.ModelAdmin):
    list_display = ('account_holder_full_name', 'user', 'masked_display', 'is_default', 'created_at')
    list_filter = ('is_default',)
    list_select_related = ('user',)
    search_fields = ('user__phone_number', 'account_holder_first_name', 'account_holder_last_name', 'card_number', 'iban')
    autocomplete_fields = ('user',)
    actions = ('make_default',)

    @admin.action(description='تنظیم به عنوان حساب پیش‌فرض کاربر (فقط یک حساب انتخاب شود)')
    def make_default(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, 'برای این عملیات دقیقاً یک حساب انتخاب کنید.', messages.ERROR)
            return
        queryset.get().set_default()
        self.message_user(request, 'حساب بانکی پیش‌فرض کاربر تغییر کرد.', messages.SUCCESS)


@admin.register(CustomUser)
class CustomUserAdmin(admin.ModelAdmin):
    inlines = (AddressInline, UserBankAccountInline)
    # اضافه شدن نام و نام خانوادگی به لیست اصلی
    list_display = ('phone_number', 'get_full_name', 'colored_status', 'colored_approval_status', 'erp_code', 'date_joined', 'is_active')

    list_filter = ('status', 'approval_status', 'can_purchase_with_check', 'is_active', 'is_staff', 'date_joined')

    # اضافه شدن کد ملی و نام به باکس جستجو
    search_fields = ('phone_number', 'erp_code', 'national_code', 'first_name', 'last_name', 'business_name')

    ordering = ('-date_joined',)

    # approval_status/approved_at/approved_by عمداً readonly: تنها راه مجاز تغییرشان اکشن‌های
    # approve_selected/reject_selected (که از متد مدل CustomUser.approve()/reject() عبور
    # می‌کنند) است، نه دراپ‌داون دستی در همین فرم — طبق تصمیم معماریِ تأییدشده.
    readonly_fields = ('date_joined', 'last_login', 'retry_count', 'last_sync_error',
                       'imported_from_holoo', 'holoo_full_name', 'holoo_customer_code', 'holoo_bed_sarfasl',
                       'approval_status', 'approved_at', 'approved_by', 'rejected_by', 'cheque_credit_latest', 'cheque_credit_usage')

    actions = ('approve_selected', 'reject_selected')
    change_list_template = 'admin/accounts/customuser/change_list.html'

    def get_urls(self):
        custom = [path('import-holoo/', self.admin_site.admin_view(self.import_holoo_view), name='accounts_customuser_import_holoo')]
        return custom + super().get_urls()

    def import_holoo_view(self, request):
        """
        ورود مشتریان هلو به‌عنوان کاربر سایت (holoo/customers.py). GET پیش‌نمایش می‌دهد (چیزی نمی‌نویسد)، POST اجرا می‌کند؛
        ?csv=1 گزارش کامل را دانلود می‌کند. فقط برای دارندگان مجوز افزودن کاربر.
        """
        if not self.has_add_permission(request):
            raise PermissionDenied
        from holoo.client import HolooClient
        from holoo.customers import (OUTCOME_LABELS, REASON_LABELS, apply_plan, build_plan, csv_rows,
                                     fetch_customer_rows)

        context = {**self.admin_site.each_context(request), 'opts': self.model._meta, 'title': 'ورود مشتریان هلو'}
        rows = fetch_customer_rows(HolooClient())
        if rows is None:
            context['error'] = 'خواندن مشتریان از هلو ممکن نشد (اتصال هلو یا حالت mock/غیرفعال را بررسی کنید).'
            return TemplateResponse(request, 'admin/accounts/customuser/import_holoo.html', context)

        plan = build_plan(rows)
        applied = request.method == 'POST'
        report = apply_plan(plan, apply=applied)
        if request.GET.get('csv'):
            response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
            response['Content-Disposition'] = 'attachment; filename="holoo_customers_report.csv"'
            response.write('﻿')
            writer = csv.writer(response)
            writer.writerow(['کد هلو', 'نام', 'موبایل', 'نتیجه', 'توضیح'])
            writer.writerows(csv_rows(plan, report))
            return response
        context.update(
            total=len(rows), candidates=len(plan.candidates), applied=applied,
            skipped=[(REASON_LABELS[reason], count) for reason, count in plan.reason_counts().most_common()],
            outcomes=[(OUTCOME_LABELS.get(key, key), count) for key, count in report.counts.most_common()],
            problems=[e for e in report.entries if e['outcome'] in ('conflict', 'error')][:200],
        )
        if applied:
            messages.success(request, 'ورود مشتریان هلو انجام شد.')
        return TemplateResponse(request, 'admin/accounts/customuser/import_holoo.html', context)

    # دسته‌بندی جدید و بسیار مرتب فیلدها در صفحه ویرایش
    fieldsets = (
        ('اطلاعات ورود و پایه', {
            'fields': ('phone_number', 'status', 'erp_code')
        }),
        ('اطلاعات هویتی', {
            'fields': ('first_name', 'last_name', 'national_code', 'business_name')
        }),
        ('تأیید تجاری (مستقل از وضعیت هلوی بالا؛ فقط با اکشن‌های تأیید/رد تغییر می‌کند)', {
            'fields': ('approval_status', 'price_level', 'can_purchase_with_check', 'cheque_credit_limit', 'cheque_credit_usage',
                      'cheque_credit_latest', 'approved_at', 'approved_by', 'rejected_by', 'rejection_reason'),
        }),
        ('وضعیت یکپارچه‌سازی هلو', {
            'fields': ('imported_from_holoo', 'holoo_full_name', 'holoo_customer_code', 'holoo_bed_sarfasl', 'retry_count', 'last_sync_error')
        }),
        ('دسترسی‌ها و تاریخ‌ها', {
            'fields': ('is_active', 'is_staff', 'is_superuser', 'date_joined', 'last_login')
        }),
    )

    def get_fieldsets(self, request, obj=None):
        """ پیوند درخواست خرید چکی فقط برای کسی که مجوز مشاهده‌ی درخواست‌ها را دارد (و فقط در صفحه‌ی کاربرِ موجود) """
        fieldsets = super().get_fieldsets(request, obj)
        if obj is not None and request.user.has_perm('accounts.view_chequecreditrequest'):
            return fieldsets
        return [(title, {**opts, 'fields': tuple(f for f in opts['fields'] if f != 'cheque_credit_latest')}) for title, opts in fieldsets]

    @admin.display(description='اعتبار چکیِ مصرف‌شده / باقی‌مانده')
    def cheque_credit_usage(self, obj):
        """ مصرف‌شده = جمع سفارش‌های چکیِ تسویه‌نشده‌ی سایت (orders/credit.py)؛ فروش حضوری هلو در آن نیست """
        if not obj.pk:
            return '—'
        from orders import credit
        from orders.templatetags.money import money
        state = credit.credit_state(obj)
        if state.unlimited:
            return f'مصرف‌شده {money(state.used)} تومان — بدون سقف'
        if state.frozen:
            return f'مصرف‌شده {money(state.used)} تومان — اعتبار فریز است'
        return f'مصرف‌شده {money(state.used)} از {money(state.limit)} تومان — باقی‌مانده {money(state.remaining)} تومان'

    @admin.display(description='آخرین درخواست خرید چکی')
    def cheque_credit_latest(self, obj):
        if not obj.pk:
            return '—'
        requests = list(obj.cheque_credit_requests.order_by('-created_at', '-id')[:1])
        if not requests:
            return 'درخواستی ثبت نشده است'
        latest = requests[0]
        total = obj.cheque_credit_requests.count()
        return format_html('<a href="{}">درخواست #{} — {}</a> ({}؛ مجموع {} درخواست)',
                           reverse('admin:accounts_chequecreditrequest_change', args=[latest.pk]), latest.pk, latest.get_status_display(),
                           timezone.localtime(latest.created_at).strftime('%Y-%m-%d %H:%M'), total)

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


# ---------------------------------------------------------------------------------------------------------------------------
# درخواست خرید چکی / اعتباری (فاز F3). منطق تأیید/رد فقط در accounts/cheque_credit_service.py است؛ ادمین فقط فرم و اکشن‌ها را به آن می‌رساند.
# ---------------------------------------------------------------------------------------------------------------------------

def _mask_national_code(code):
    """ «۱۲۳۴۵۶۷۸۹۰» ← «123*****90» (لیست ادمین؛ کد کامل فقط در صفحه‌ی بررسی) """
    code = str(code or '')
    return f'{code[:3]}{"*" * max(len(code) - 5, 0)}{code[-2:]}' if len(code) >= 6 else '—'


class ChequeCreditReviewForm(forms.ModelForm):
    """ فرم تصمیم مدیر: وضعیت (تأیید/رد)، علت رد (برای رد الزامی)، سقف تأییدشده و یادداشت داخلی. تأیید/رد را سرویس اتمیک اجرا می‌کند. """

    class Meta:
        model = ChequeCreditRequest
        fields = ['status', 'rejection_reason', 'approved_limit', 'admin_note']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'status' in self.fields:
            allowed = (ChequeCreditRequest.STATUS_PENDING, ChequeCreditRequest.STATUS_APPROVED, ChequeCreditRequest.STATUS_REJECTED)
            self.fields['status'].choices = [c for c in ChequeCreditRequest.STATUS_CHOICES if c[0] in allowed]
            self.fields['status'].label = 'تصمیم'
        if 'rejection_reason' in self.fields:
            self.fields['rejection_reason'].widget = forms.Textarea(attrs={'rows': 3, 'cols': 70, 'maxlength': 300})
            self.fields['rejection_reason'].help_text = 'فقط برای رد؛ الزامی است و همین متن به مشتری (و در پیامک) نمایش داده می‌شود.'
        if 'admin_note' in self.fields:
            self.fields['admin_note'].widget = forms.Textarea(attrs={'rows': 2, 'cols': 70, 'maxlength': 500})
        if 'approved_limit' in self.fields:
            self.fields['approved_limit'].label = 'سقف اعتبار تأییدشده (تومان)'
            self.fields['approved_limit'].help_text = ('برای تأیید الزامی است (پیش‌فرض: سقف درخواستی مشتری)؛ در تسویه‌حساب اعمال می‌شود و '
                                                       'روی کاربر ثبت می‌گردد. ۰ = فریز.')
            instance = kwargs.get('instance')
            if instance is not None and instance.pk and instance.approved_limit is None:
                self.initial['approved_limit'] = instance.requested_limit

    def clean(self):
        cleaned = super().clean()
        status = cleaned.get('status')
        if status == ChequeCreditRequest.STATUS_REJECTED and not (cleaned.get('rejection_reason') or '').strip():
            self.add_error('rejection_reason', 'برای رد درخواست، علت رد را بنویسید.')
        limit = cleaned.get('approved_limit')
        if status == ChequeCreditRequest.STATUS_APPROVED:
            if limit is None:
                self.add_error('approved_limit', 'برای تأیید، سقف اعتبار را مشخص کنید (پیش‌فرض: سقف درخواستی مشتری).')
            elif limit <= 0:
                self.add_error('approved_limit', 'سقف تأییدشده باید بیشتر از صفر باشد.')
        return cleaned


class ChequeCreditStatusFilter(admin.SimpleListFilter):
    """ فیلتر وضعیت با پیش‌فرضِ «همه»؛ «در انتظار بررسی» اولین گزینه است تا صف بررسی یک کلیک باشد """
    title = 'وضعیت'
    parameter_name = 'status'

    def lookups(self, request, model_admin):
        return ChequeCreditRequest.STATUS_CHOICES

    def queryset(self, request, queryset):
        return queryset.filter(status=self.value()) if self.value() else queryset


@admin.register(ChequeCreditRequest)
class ChequeCreditRequestAdmin(admin.ModelAdmin):
    """
    بررسی درخواست‌های خرید چکی. اطلاعات هویتی و اعتباری فقط‌خواندنی؛ مدارک از ویوی امنِ همین ادمین (با مجوز مشاهده) و با هدرهای
    nosniff/CSP sandbox نمایش داده می‌شوند. تصمیم (تأیید/رد با علت) از فرم همین صفحه یا اکشن‌های لیست؛ هر دو سرویس اتمیک را صدا می‌زنند
    (تأیید ← can_purchase_with_check کاربر روشن می‌شود). تصمیم نهایی است و بعدش فقط‌خواندنی. افزودن/حذف ممنوع.
    """
    form = ChequeCreditReviewForm
    list_display = ['id', 'user_link', 'full_name', 'masked_national_code', 'business_name', 'requested_limit', 'status_badge',
                    'created_at', 'reviewed_by']
    list_display_links = ['id']
    list_filter = [ChequeCreditStatusFilter, 'created_at']
    search_fields = ['user__phone_number', 'first_name', 'last_name', 'national_code', 'business_name']
    list_select_related = ['user', 'reviewed_by']
    date_hierarchy = 'created_at'
    actions = ['approve_requests', 'reject_requests']
    fieldsets = (
        ('درخواست', {'fields': ('user_link', 'status_badge', 'created_at', 'user_permission_state')}),
        ('هویت (اسنپ‌شات پروفایل در لحظه‌ی ثبت)', {'fields': ('first_name', 'last_name', 'national_code')}),
        ('اطلاعات اعتباری', {'fields': ('business_name', 'bank_name', 'account_holder', 'iban', 'requested_limit', 'monthly_turnover',
                                        'description')}),
        ('مدارک', {'fields': ('documents_preview',)}),
        ('تصمیم مدیر', {'fields': ('status', 'rejection_reason', 'approved_limit', 'admin_note'),
                        'description': 'تأیید، مجوز خرید چکی کاربر را خودکار فعال می‌کند. رد بدون علت ممکن نیست. تصمیم نهایی است.'}),
        ('سوابق بررسی', {'fields': ('reviewed_by', 'reviewed_at', 'decided_at', 'documents_purged_at', 'updated_at')}),
    )
    static_readonly = ['user_link', 'status_badge', 'created_at', 'user_permission_state', 'first_name', 'last_name', 'national_code',
                       'business_name', 'bank_name', 'account_holder', 'iban', 'requested_limit', 'monthly_turnover', 'description',
                       'documents_preview', 'reviewed_by', 'reviewed_at', 'decided_at', 'documents_purged_at', 'updated_at']

    # ---- فقط‌خواندنی‌ها: پس از تصمیم، فرم تصمیم هم قفل است ----
    def get_object(self, request, object_id, from_field=None):
        obj = super().get_object(request, object_id, from_field)
        if obj is not None:
            obj._persisted_status = obj.status          # فرمِ نامعتبر، status نمونه را در حافظه عوض می‌کند؛ قفل‌شدن باید از وضعیت دیتابیس باشد
        return obj

    def render_change_form(self, request, context, add=False, change=False, form_url='', obj=None):
        # فرم نامعتبر وضعیتِ پیشنهادیِ کاربر را روی نمونه می‌نشاند؛ عنوان صفحه نباید وضعیتی را نشان دهد که ذخیره نشده
        if obj is not None and hasattr(obj, '_persisted_status'):
            obj.status = obj._persisted_status
            if 'subtitle' in context:                      # عنوان صفحه پیش از این‌جا از str(obj) (با وضعیت تغییرکرده) ساخته شده است
                context['subtitle'] = str(obj)
        return super().render_change_form(request, context, add=add, change=change, form_url=form_url, obj=obj)

    def get_readonly_fields(self, request, obj=None):
        readonly = list(self.static_readonly)
        if obj is not None and getattr(obj, '_persisted_status', obj.status) != ChequeCreditRequest.STATUS_PENDING:
            readonly += ['status', 'rejection_reason', 'approved_limit', 'admin_note']
        return readonly

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('user', 'reviewed_by').prefetch_related('documents')

    # ---- ستون‌ها ----
    @admin.display(description='کاربر', ordering='user__phone_number')
    def user_link(self, obj):
        return format_html('<a href="{}">{}</a>', reverse('admin:accounts_customuser_change', args=[obj.user_id]), obj.user.phone_number)

    @admin.display(description='نام و نام خانوادگی')
    def full_name(self, obj):
        return f'{obj.first_name} {obj.last_name}'.strip() or '—'

    @admin.display(description='کد ملی', ordering='national_code')
    def masked_national_code(self, obj):
        return _mask_national_code(obj.national_code)

    @admin.display(description='وضعیت', ordering='status')
    def status_badge(self, obj):
        colors = {'pending': '#ff9800', 'approved': '#4caf50', 'rejected': '#f44336', 'canceled': '#9e9e9e'}
        return format_html('<span style="background-color:{};color:white;padding:3px 10px;border-radius:12px;font-weight:bold;font-size:11px;">{}</span>',
                           colors.get(obj.status, '#000'), obj.get_status_display())

    @admin.display(description='مجوز خرید چکیِ کاربر (اکنون)')
    def user_permission_state(self, obj):
        user = obj.user
        if not user.can_purchase_with_check:
            return 'غیرفعال'
        from orders.templatetags.money import money
        return 'فعال — ' + ('بدون سقف' if user.cheque_credit_limit is None else f'سقف {money(user.cheque_credit_limit)} تومان')

    @admin.display(description='مدارک آپلودشده')
    def documents_preview(self, obj):
        if obj.documents_purged_at:
            return 'تصاویر مدارک طبق سیاست نگهداری پاک شده‌اند.'
        docs = list(obj.documents.all())
        if not docs:
            return 'مدرکی ثبت نشده است.'
        cells = []
        for doc in docs:
            url = reverse('admin:accounts_chequecreditrequest_document', args=[doc.public_id])
            cells.append(format_html(
                '<a href="{0}" target="_blank" rel="noopener" style="display:inline-block;margin:0 0 8px 8px;text-align:center;">'
                '<img src="{0}" alt="{1}" loading="lazy" style="width:140px;height:140px;object-fit:cover;border:1px solid #ccc;border-radius:6px;"><br>{1}</a>',
                url, doc.get_kind_display()))
        return format_html_join('', '{}', ((c,) for c in cells))

    # ---- نمایش امن مدارک ----
    def get_urls(self):
        custom = [path('document/<uuid:document_id>/', self.admin_site.admin_view(self.document_view),
                       name='accounts_chequecreditrequest_document')]
        return custom + super().get_urls()

    def document_view(self, request, document_id):
        """ مدرک فقط برای دارندگان مجوز مشاهده‌ی درخواست؛ هدرهای امنیتی را document_response می‌گذارد. نبودِ فایل ← ۴۰۴ """
        if not self.has_view_permission(request):
            raise PermissionDenied
        document = ChequeCreditDocument.objects.filter(public_id=document_id).first()
        response = credit_service.document_response(document) if document else None
        if response is None:
            raise Http404
        return response

    # ---- ذخیره‌ی تصمیم از فرم صفحه ----
    def save_model(self, request, obj, form, change):
        status = form.cleaned_data.get('status')
        note = form.cleaned_data.get('admin_note') or ''
        try:
            if status == ChequeCreditRequest.STATUS_APPROVED:
                limit = form.cleaned_data.get('approved_limit')
                credit_service.approve_request(obj, request.user, approved_limit=int(limit) if limit is not None else None, admin_note=note)
                self.message_user(request, 'درخواست تأیید شد و مجوز خرید چکی برای کاربر فعال شد.', messages.SUCCESS)
            elif status == ChequeCreditRequest.STATUS_REJECTED:
                credit_service.reject_request(obj, request.user, form.cleaned_data.get('rejection_reason'), admin_note=note)
                self.message_user(request, 'درخواست رد شد و علت برای مشتری ثبت شد.', messages.SUCCESS)
            else:
                credit_service.set_admin_note(obj, note)
        except credit_service.ChequeCreditError as error:
            self.message_user(request, error.message, messages.ERROR)

    # ---- اکشن‌ها ----
    @admin.action(description='تأیید درخواست‌های انتخاب‌شده (فعال‌سازی مجوز خرید چکی)')
    def approve_requests(self, request, queryset):
        if not self.has_change_permission(request):
            self.message_user(request, 'اجازه‌ی بررسی درخواست را ندارید.', messages.ERROR)
            return
        done = 0
        for credit_request in queryset:
            try:
                credit_service.approve_request(credit_request, request.user)
                done += 1
            except credit_service.ChequeCreditError as error:
                self.message_user(request, f'درخواست #{credit_request.pk}: {error.message}', messages.WARNING)
        if done:
            self.message_user(request, f'{done} درخواست تأیید و مجوز خرید چکی کاربرانشان فعال شد.', messages.SUCCESS)

    @admin.action(description='رد درخواست‌های انتخاب‌شده (با علت)')
    def reject_requests(self, request, queryset):
        """ صفحه‌ی میانی: علت رد الزامی است و برای همه‌ی درخواست‌های انتخاب‌شده ثبت می‌شود """
        if not self.has_change_permission(request):
            self.message_user(request, 'اجازه‌ی بررسی درخواست را ندارید.', messages.ERROR)
            return None
        reason = (request.POST.get('reason') or '').strip()
        if request.POST.get('apply') and reason:
            done = 0
            for credit_request in queryset:
                try:
                    credit_service.reject_request(credit_request, request.user, reason)
                    done += 1
                except credit_service.ChequeCreditError as error:
                    self.message_user(request, f'درخواست #{credit_request.pk}: {error.message}', messages.WARNING)
            if done:
                self.message_user(request, f'{done} درخواست رد شد و علت برای مشتری ثبت شد.', messages.SUCCESS)
            return None
        context = {
            **self.admin_site.each_context(request), 'opts': self.model._meta, 'title': 'رد درخواست‌های خرید چکی',
            'requests': queryset, 'action_name': 'reject_requests', 'reason': reason,
            'error': 'علت رد را بنویسید.' if request.POST.get('apply') else '',
            'select_across': request.POST.get('select_across', '0'),
            'selected': request.POST.getlist(helpers.ACTION_CHECKBOX_NAME),
        }
        return TemplateResponse(request, 'admin/accounts/chequecreditrequest/reject_reason.html', context)
