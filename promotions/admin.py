"""
ادمین تخفیف‌ها: سیاست سراسری (تک‌ردیفی) و تخفیف‌های خودکار با اهداف (محصول/دسته/برند/کل فروشگاه)، فیلتر وضعیت، عملیات
گروهی (فعال/غیرفعال/تمدید/کپی)، پیش‌نمایش «چند محصول مشمول است» و هشدار هم‌پوشانی با تخفیف‌های دیگر.
"""

import csv
import re
from datetime import timedelta

import jdatetime
from django import forms
from django.contrib import admin, messages
from django.contrib.admin import helpers
from django.db import models, transaction
from django.http import HttpResponse
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html

from products.models import Product
from services.jalali_widgets import JalaliSplitDateTimeField

from .flash import rule_product_filter
from .index import category_children_map, make_rule
from .models import (
    Coupon, CouponRedemption, DiscountPolicy, FreeShippingRule, Promotion, PromotionTarget, UserCoupon, generate_code,
    normalize_code,
)

STATUS_COLORS = {
    'active': '#16a34a', 'scheduled': '#2563eb', 'expired': '#6b7280', 'inactive': '#dc2626',
}


def _jalali(value):
    if not value:
        return '-'
    if timezone.is_aware(value):
        value = timezone.localtime(value)
    return jdatetime.datetime.fromgregorian(datetime=value).strftime('%Y/%m/%d %H:%M')


def promotion_products(promotion):
    """ کوئری‌ست محصولات قابل‌نمایشِ مشمول یک تخفیف ذخیره‌شده (برای پیش‌نمایش و هشدار هم‌پوشانی) """
    targets = list(promotion.targets.select_related('product', 'category', 'brand'))
    rule = make_rule(promotion, targets, category_children_map())
    return Product.visible.filter(rule_product_filter(rule))


def overlapping_promotions(promotion, limit=30):
    """ [(تخفیف دیگر، تعداد محصولاتِ مشترک)] برای تخفیف‌های فعالی که بازه‌ی زمانی‌شان با این یکی هم‌پوشان است """
    others = (Promotion.objects.filter(is_active=True, starts_at__lt=promotion.ends_at, ends_at__gt=promotion.starts_at)
              .exclude(pk=promotion.pk).order_by('-priority', 'id')[:limit])
    mine = promotion_products(promotion)
    result = []
    for other in others:
        common = mine.filter(pk__in=promotion_products(other).values('pk')).count()
        if common:
            result.append((other, common))
    return result


# ---------------------------------------------------------------- سیاست سراسری
@admin.register(DiscountPolicy)
class DiscountPolicyAdmin(admin.ModelAdmin):
    """ تک‌ردیفی؛ لیست همیشه مستقیم به فرم ویرایش همان یک ردیف می‌رود (مثل تنظیمات سایت) """
    fieldsets = (
        ('تخفیف‌های خودکار', {'fields': ('promotions_enabled',)}),
        ('چه کسانی و با چه روشی', {'fields': ('apply_to_vip', 'apply_for_cash', 'apply_for_check')}),
        ('ترکیب، سقف و گرد کردن', {'fields': ('promotion_stacking', 'max_item_discount_percent', 'rounding_step')}),
        ('ارسال رایگان', {
            'fields': ('free_shipping_rules_enabled', 'free_shipping_threshold_after_coupon'),
            'description': 'کلید سراسری قاعده‌های ارسال رایگان و مبنای سنجش حداقل مبلغ سبد. مبلغ حداقل، بازه‌ی زمانی، محدوده‌ی جغرافیایی '
                           'و فعال/غیرفعال‌بودنِ هر قاعده در «قاعده‌های ارسال رایگان» تنظیم می‌شود.',
        }),
        ('کدهای تخفیف (کوپن)', {
            'fields': ('coupon_reservation_minutes', 'coupon_max_invalid_attempts', 'coupon_attempt_window_minutes'),
            'description': 'مهلت رزرو ظرفیت کد پس از ثبت سفارش، و سقف تلاش ناموفق برای وارد کردن کد (برای هر کاربر و هر IP).',
        }),
    )

    def has_add_permission(self, request):
        return not DiscountPolicy.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = DiscountPolicy.load()
        return redirect(reverse('admin:promotions_discountpolicy_change', args=[obj.pk]))


# ---------------------------------------------------------------- تخفیف خودکار
class PromotionTargetFormSet(forms.BaseInlineFormSet):
    def clean(self):
        super().clean()
        includes = [
            f for f in self.forms
            if f.cleaned_data and not f.cleaned_data.get('DELETE') and not f.cleaned_data.get('is_exclusion')
        ]
        if not includes:
            raise forms.ValidationError(
                'حداقل یک هدفِ «شمول» لازم است (محصول، دسته، برند یا «کل فروشگاه»).'
            )


class PromotionTargetInline(admin.TabularInline):
    model = PromotionTarget
    formset = PromotionTargetFormSet
    extra = 1
    fields = ('target_type', 'product', 'category', 'brand', 'include_descendants', 'is_exclusion')
    autocomplete_fields = ('product', 'category', 'brand')


class PromotionStatusFilter(admin.SimpleListFilter):
    title = 'وضعیت'
    parameter_name = 'status'

    def lookups(self, request, model_admin):
        return [(key, label) for key, label in Promotion.STATUS_LABELS.items()]

    def queryset(self, request, queryset):
        now = timezone.now()
        value = self.value()
        if value == Promotion.STATUS_INACTIVE:
            return queryset.filter(is_active=False)
        if value == Promotion.STATUS_SCHEDULED:
            return queryset.filter(is_active=True, starts_at__gt=now)
        if value == Promotion.STATUS_ACTIVE:
            return queryset.filter(is_active=True, starts_at__lte=now, ends_at__gte=now)
        if value == Promotion.STATUS_EXPIRED:
            return queryset.filter(is_active=True, ends_at__lt=now)
        return queryset


@admin.register(Promotion)
class PromotionAdmin(admin.ModelAdmin):
    list_display = ('title', 'kind_and_value', 'target_summary', 'schedule', 'status_badge', 'priority', 'is_active')
    list_editable = ('priority', 'is_active')
    list_filter = (PromotionStatusFilter, 'kind', 'show_in_flash_deals', 'login_required')
    search_fields = ('title', 'targets__product__name', 'targets__category__name', 'targets__brand__name')
    inlines = [PromotionTargetInline]
    actions = ('activate', 'deactivate', 'extend_7_days', 'extend_30_days', 'duplicate')
    date_hierarchy = 'starts_at'
    formfield_overrides = {models.DateTimeField: {'form_class': JalaliSplitDateTimeField}}
    readonly_fields = ('status_badge', 'affected_preview', 'created_at', 'updated_at')

    fieldsets = (
        ('اطلاعات تخفیف', {'fields': ('title', 'kind', 'value', 'max_discount_amount')}),
        ('زمان‌بندی', {'fields': ('starts_at', 'ends_at', 'is_active', 'priority', 'status_badge')}),
        ('مخاطب و شرایط', {
            'fields': ('login_required', 'min_loyalty_level', 'price_levels', 'payment_method'),
            'description': 'خالی/پیش‌فرض یعنی بدون محدودیت. کاربران سطح ویژه و روش‌های پرداخت با «سیاست تخفیف» هم کنترل می‌شوند.',
        }),
        ('نمایش', {'fields': ('show_in_flash_deals', 'badge_label')}),
        ('پیش‌نمایش (پس از ذخیره)', {'fields': ('affected_preview', 'created_at', 'updated_at')}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related('targets__product', 'targets__category', 'targets__brand')

    # ----- ستون‌ها -----
    @admin.display(description='نوع و مقدار')
    def kind_and_value(self, obj):
        return f'{obj.get_kind_display()} — {obj.value_display}'

    @admin.display(description='هدف')
    def target_summary(self, obj):
        parts = [str(t) for t in obj.targets.all()]
        text = '، '.join(parts[:3]) + (f' (+{len(parts) - 3})' if len(parts) > 3 else '')
        return text or '⚠ بدون هدف'

    @admin.display(description='بازه (شمسی)')
    def schedule(self, obj):
        return f'{_jalali(obj.starts_at)} ← {_jalali(obj.ends_at)}'

    @admin.display(description='وضعیت')
    def status_badge(self, obj):
        if not obj.pk:
            return '-'
        status = obj.status()
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 10px;border-radius:12px;font-size:11px;font-weight:bold">{}</span>',
            STATUS_COLORS[status], Promotion.STATUS_LABELS[status],
        )

    @admin.display(description='محصولات مشمول')
    def affected_preview(self, obj):
        if not obj.pk:
            return 'پس از ذخیره‌ی تخفیف و اهدافش نمایش داده می‌شود.'
        qs = promotion_products(obj)
        count = qs.count()
        if not count:
            return format_html('<b style="color:#dc2626">هیچ محصول قابل‌نمایشی مشمول نیست.</b>')
        sample = '، '.join(qs.order_by('name').values_list('name', flat=True)[:5])
        return format_html('<b>{}</b> محصول مشمول است. نمونه: {}', count, sample)

    # ----- ذخیره + هشدار هم‌پوشانی -----
    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        promotion = form.instance
        if not promotion.is_active:
            return
        overlaps = overlapping_promotions(promotion)
        if overlaps:
            summary = '؛ '.join(f'«{other.title}» ({count} محصول مشترک)' for other, count in overlaps[:5])
            self.message_user(
                request,
                f'توجه: این تخفیف با تخفیف‌های دیگری که هم‌زمان فعال‌اند روی محصولات مشترک هم‌پوشانی دارد: {summary}. '
                f'طبق «سیاست تخفیف»، در حالت «بهترین» فقط تخفیفِ به‌صرفه‌تر اعمال می‌شود.',
                messages.WARNING,
            )

    # ----- عملیات گروهی -----
    @admin.action(description='فعال کردن')
    def activate(self, request, queryset):
        count = queryset.update(is_active=True)
        self._invalidate()
        self.message_user(request, f'{count} تخفیف فعال شد.', messages.SUCCESS)

    @admin.action(description='غیرفعال کردن')
    def deactivate(self, request, queryset):
        count = queryset.update(is_active=False)
        self._invalidate()
        self.message_user(request, f'{count} تخفیف غیرفعال شد.', messages.SUCCESS)

    def _extend(self, request, queryset, days):
        for promotion in queryset:
            promotion.ends_at = promotion.ends_at + timedelta(days=days)
            promotion.save(update_fields=['ends_at', 'updated_at'])
        self.message_user(request, f'پایان {queryset.count()} تخفیف {days} روز تمدید شد.', messages.SUCCESS)

    @admin.action(description='تمدید ۷ روزه')
    def extend_7_days(self, request, queryset):
        self._extend(request, queryset, 7)

    @admin.action(description='تمدید ۳۰ روزه')
    def extend_30_days(self, request, queryset):
        self._extend(request, queryset, 30)

    @admin.action(description='ساخت کپی (غیرفعال) با همان اهداف')
    def duplicate(self, request, queryset):
        for promotion in queryset.prefetch_related('targets'):
            targets = list(promotion.targets.all())
            promotion.pk = None
            promotion.title = f'{promotion.title} (کپی)'[:200]
            promotion.is_active = False
            promotion.save()
            PromotionTarget.objects.bulk_create([
                PromotionTarget(promotion=promotion, target_type=t.target_type, product_id=t.product_id,
                                category_id=t.category_id, brand_id=t.brand_id,
                                include_descendants=t.include_descendants, is_exclusion=t.is_exclusion)
                for t in targets
            ])
        self._invalidate()
        self.message_user(request, 'کپی‌ها به‌صورت غیرفعال ساخته شدند؛ پس از بازبینی فعالشان کنید.', messages.SUCCESS)

    @staticmethod
    def _invalidate():
        # update() و bulk_create سیگنال post_save نمی‌فرستند؛ کش شاخص را دستی باطل می‌کنیم
        from .index import invalidate
        invalidate()


# ======================================================================================== کدهای تخفیف
class OpenWindowStatusFilter(admin.SimpleListFilter):
    """ فیلتر وضعیت برای مدل‌هایی که بازه‌ی زمانی‌شان (یک‌طرفه هم) اختیاری است: کوپن و قاعده‌ی ارسال رایگان """
    title = 'وضعیت'
    parameter_name = 'status'

    def lookups(self, request, model_admin):
        return list(Coupon.STATUS_LABELS.items())

    def queryset(self, request, queryset):
        now = timezone.now()
        value = self.value()
        if value == Coupon.STATUS_INACTIVE:
            return queryset.filter(is_active=False)
        if value == Coupon.STATUS_SCHEDULED:
            return queryset.filter(is_active=True, starts_at__gt=now)
        if value == Coupon.STATUS_EXPIRED:
            return queryset.filter(is_active=True, ends_at__lt=now)
        if value == Coupon.STATUS_ACTIVE:
            return (queryset.filter(is_active=True)
                    .filter(models.Q(starts_at__isnull=True) | models.Q(starts_at__lte=now))
                    .filter(models.Q(ends_at__isnull=True) | models.Q(ends_at__gte=now)))


def _status_badge(obj):
    if not obj.pk:
        return '-'
    status = obj.status()
    return format_html(
        '<span style="background:{};color:#fff;padding:2px 10px;border-radius:12px;font-size:11px;font-weight:bold">{}</span>',
        STATUS_COLORS[status], Coupon.STATUS_LABELS[status],
    )


class CouponAdminForm(forms.ModelForm):
    class Meta:
        model = Coupon
        fields = '__all__'

    def clean(self):
        data = super().clean()
        if data.get('scope') == Coupon.SCOPE_ITEMS and not data.get('products') and not data.get('categories'):
            raise forms.ValidationError('برای شمول «محصولات/دسته‌های انتخاب‌شده» حداقل یک محصول یا دسته انتخاب کنید.')
        return data


class UserCouponInline(admin.TabularInline):
    model = UserCoupon
    extra = 0
    fields = ('user', 'source', 'created_at')
    readonly_fields = ('created_at',)
    raw_id_fields = ('user',)


class CouponRedemptionInline(admin.TabularInline):
    model = CouponRedemption
    extra = 0
    can_delete = False
    fields = ('order_id', 'user', 'status', 'discount_amount', 'shipping_discount', 'reserved_at', 'expires_at', 'over_limit')
    readonly_fields = fields
    ordering = ('-reserved_at',)
    max_num = 0
    verbose_name_plural = 'مصرف‌ها (فقط‌خواندنی)'

    def has_add_permission(self, request, obj=None):
        return False


class BulkGenerateForm(forms.Form):
    prefix = forms.CharField(label='پیشوند', max_length=20, required=False, help_text='مثلاً YALDA ← YALDA-K7M2QX9A')
    count = forms.IntegerField(label='تعداد کد', min_value=1, max_value=5000, initial=100)
    length = forms.IntegerField(label='طول بخش تصادفی', min_value=6, max_value=16, initial=8)
    total_limit = forms.IntegerField(label='سقف کل مصرف هر کد', min_value=1, required=False, initial=1,
                                     help_text='خالی = مثل کد الگو. برای کدهای یک‌بارمصرف ۱ بگذارید.')

    def clean_prefix(self):
        prefix = normalize_code(self.cleaned_data.get('prefix')).strip('-')
        if prefix and not re.fullmatch(r'[A-Z0-9_]{1,20}', prefix):
            raise forms.ValidationError('پیشوند فقط حروف لاتین، رقم و «_» باشد.')
        return prefix


@admin.register(Coupon)
class CouponAdmin(admin.ModelAdmin):
    form = CouponAdminForm
    list_display = ('code', 'title', 'kind_and_value', 'status_badge', 'usage', 'discount_given', 'schedule', 'is_active')
    list_filter = (OpenWindowStatusFilter, 'kind', 'first_order_only', 'audience', 'allow_with_promotions')
    search_fields = ('code', 'title')
    actions = ('activate', 'deactivate', 'extend_7_days', 'export_csv', 'bulk_generate')
    inlines = [UserCouponInline, CouponRedemptionInline]
    autocomplete_fields = ('products', 'categories')
    formfield_overrides = {models.DateTimeField: {'form_class': JalaliSplitDateTimeField}}
    readonly_fields = ('status_badge', 'usage_report', 'created_at', 'updated_at')

    fieldsets = (
        ('کد', {'fields': ('code', 'title', 'description', 'is_active', 'status_badge')}),
        ('نوع و مقدار', {
            'fields': ('kind', 'value', 'max_discount_amount'),
            'description': 'درصدی: value = درصد (سقف مبلغ اختیاری) — مبلغ ثابت: value = تومان — ارسال رایگان: فقط برای پیک درون‌شهری؛ value نادیده گرفته می‌شود.',
        }),
        ('شمول و شرط‌ها', {
            'fields': ('scope', 'products', 'categories', 'min_cart_amount', 'allow_with_promotions'),
            'description': 'حداقل مبلغ سبد پس از کسر تخفیف‌های خودکار سنجیده می‌شود. «ترکیب با تخفیف خودکار» خاموش = کد فقط روی اقلامِ بدون تخفیف خودکار اعمال می‌شود.',
        }),
        ('اعتبار و سقف‌ها', {'fields': ('starts_at', 'ends_at', 'total_limit', 'per_user_limit', 'first_order_only', 'audience')}),
        ('گزارش مصرف', {'fields': ('usage_report', 'created_at', 'updated_at')}),
    )

    def get_queryset(self, request):
        live = models.Q(redemptions__status=CouponRedemption.STATUS_REDEEMED) | models.Q(
            redemptions__status=CouponRedemption.STATUS_RESERVED, redemptions__expires_at__gt=timezone.now())
        return super().get_queryset(request).annotate(
            _uses=models.Count('redemptions', filter=live, distinct=True),
            _discount=models.Sum('redemptions__discount_amount', filter=models.Q(redemptions__status=CouponRedemption.STATUS_REDEEMED)),
        )

    # ----- ستون‌ها -----
    @admin.display(description='نوع و مقدار')
    def kind_and_value(self, obj):
        return f'{obj.get_kind_display()} — {obj.value_display}'

    @admin.display(description='وضعیت')
    def status_badge(self, obj):
        return _status_badge(obj)

    @admin.display(description='مصرف', ordering='_uses')
    def usage(self, obj):
        limit = obj.total_limit if obj.total_limit is not None else '∞'
        return f'{obj._uses} از {limit}' if hasattr(obj, '_uses') else '-'

    @admin.display(description='تخفیف داده‌شده (مصرف نهایی)', ordering='_discount')
    def discount_given(self, obj):
        return f'{int(obj._discount or 0):,} تومان' if hasattr(obj, '_discount') else '-'

    @admin.display(description='بازه (شمسی)')
    def schedule(self, obj):
        return f'{_jalali(obj.starts_at)} ← {_jalali(obj.ends_at)}'

    @admin.display(description='گزارش مصرف')
    def usage_report(self, obj):
        if not obj.pk:
            return 'پس از ذخیره نمایش داده می‌شود.'
        rows = {status: (0, 0) for status, _ in CouponRedemption.STATUS_CHOICES}
        for row in obj.redemptions.values('status').annotate(n=models.Count('id'), total=models.Sum('discount_amount')):
            rows[row['status']] = (row['n'], int(row['total'] or 0))
        return format_html(
            'مصرف‌شده: <b>{}</b> ({} تومان تخفیف) — رزرو‌شده (در انتظار پرداخت): <b>{}</b> — آزادشده: <b>{}</b>',
            rows['redeemed'][0], f"{rows['redeemed'][1]:,}", rows['reserved'][0], rows['released'][0],
        )

    # ----- ذخیره -----
    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)

    # ----- عملیات گروهی -----
    @admin.action(description='فعال کردن')
    def activate(self, request, queryset):
        self.message_user(request, f'{queryset.update(is_active=True)} کد فعال شد.', messages.SUCCESS)

    @admin.action(description='غیرفعال کردن')
    def deactivate(self, request, queryset):
        self.message_user(request, f'{queryset.update(is_active=False)} کد غیرفعال شد.', messages.SUCCESS)

    @admin.action(description='تمدید ۷ روزه (فقط کدهای دارای تاریخ پایان)')
    def extend_7_days(self, request, queryset):
        count = 0
        for coupon in queryset.exclude(ends_at__isnull=True):
            coupon.ends_at += timedelta(days=7)
            coupon.save(update_fields=['ends_at', 'updated_at'])
            count += 1
        self.message_user(request, f'پایان {count} کد ۷ روز تمدید شد.', messages.SUCCESS)

    @admin.action(description='خروجی CSV (کد، عنوان، نوع، مصرف)')
    def export_csv(self, request, queryset):
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = 'attachment; filename="coupons.csv"'
        response.write('﻿')                                    # BOM: اکسل فارسی را درست بخواند
        writer = csv.writer(response)
        writer.writerow(['code', 'title', 'kind', 'value', 'status', 'uses', 'total_limit', 'per_user_limit', 'ends_at'])
        for coupon in self.get_queryset(request).filter(pk__in=queryset.values('pk')).order_by('code'):
            writer.writerow([coupon.code, coupon.title, coupon.kind, coupon.value, coupon.status(), coupon._uses,
                             coupon.total_limit if coupon.total_limit is not None else '', coupon.per_user_limit or '',
                             coupon.ends_at.isoformat() if coupon.ends_at else ''])
        return response

    @admin.action(description='ساخت گروهی کد تصادفی از روی این کد (الگو)')
    def bulk_generate(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, 'برای ساخت گروهی دقیقاً یک کد را به‌عنوان الگو انتخاب کنید.', messages.ERROR)
            return None
        template = queryset.get()
        if 'apply' in request.POST:
            form = BulkGenerateForm(request.POST)
            if form.is_valid():
                created = self._generate(template, **form.cleaned_data)
                self.message_user(request, f'{created} کد ساخته شد. برای دریافت فهرست، آن‌ها را جست‌وجو و «خروجی CSV» بزنید.', messages.SUCCESS)
                prefix = form.cleaned_data['prefix']
                return redirect(f'{reverse("admin:promotions_coupon_changelist")}?q={prefix}' if prefix else reverse('admin:promotions_coupon_changelist'))
        else:
            form = BulkGenerateForm()
        return TemplateResponse(request, 'promotions/admin/bulk_generate.html', {
            **self.admin_site.each_context(request), 'opts': self.model._meta, 'form': form, 'template_coupon': template,
            'action_checkbox_name': helpers.ACTION_CHECKBOX_NAME, 'title': 'ساخت گروهی کد تخفیف',
        })

    @transaction.atomic
    def _generate(self, template, prefix, count, length, total_limit):
        existing = set(Coupon.objects.values_list('code', flat=True))
        codes = set()
        while len(codes) < count:
            code = generate_code(prefix, length)
            if code not in existing and code not in codes:
                codes.add(code)
        product_ids = list(template.products.values_list('pk', flat=True))
        category_ids = list(template.categories.values_list('pk', flat=True))
        fields = {f.name: getattr(template, f.name) for f in Coupon._meta.concrete_fields
                  if f.name not in ('id', 'code', 'created_at', 'updated_at', 'total_limit')}
        created = Coupon.objects.bulk_create([
            Coupon(code=code, total_limit=total_limit if total_limit is not None else template.total_limit, **fields)
            for code in sorted(codes)
        ])
        # bulk_create روی SQL Server شناسه‌ها را برنمی‌گرداند؛ برای کپی M2M از دیتابیس می‌خوانیم
        if product_ids or category_ids:
            ids = list(Coupon.objects.filter(code__in=[c.code for c in created]).values_list('pk', flat=True))
            if product_ids:
                Coupon.products.through.objects.bulk_create(
                    [Coupon.products.through(coupon_id=i, product_id=p) for i in ids for p in product_ids])
            if category_ids:
                Coupon.categories.through.objects.bulk_create(
                    [Coupon.categories.through(coupon_id=i, category_id=c) for i in ids for c in category_ids])
        return len(created)


@admin.register(CouponRedemption)
class CouponRedemptionAdmin(admin.ModelAdmin):
    """ گزارش مصرف کدها (فقط‌خواندنی؛ چرخه‌ی عمر را سرویس کوپن مدیریت می‌کند) """
    list_display = ('code', 'order_link', 'user', 'status', 'discount_amount', 'shipping_discount', 'reserved_at_jalali', 'expires_at_jalali', 'over_limit')
    list_filter = ('status', 'over_limit', 'coupon')
    search_fields = ('code', 'order_id', 'user__phone_number')
    list_select_related = ('user',)
    readonly_fields = [f.name for f in CouponRedemption._meta.concrete_fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description='سفارش', ordering='order_id')
    def order_link(self, obj):
        return format_html('<a href="{}">#{}</a>', reverse('admin:orders_order_change', args=[obj.order_id]), obj.order_id)

    @admin.display(description='زمان رزرو')
    def reserved_at_jalali(self, obj):
        return _jalali(obj.reserved_at)

    @admin.display(description='پایان مهلت رزرو')
    def expires_at_jalali(self, obj):
        return _jalali(obj.expires_at)


# ======================================================================================== ارسال رایگان
@admin.register(FreeShippingRule)
class FreeShippingRuleAdmin(admin.ModelAdmin):
    list_display = ('title', 'min_total', 'scope_summary', 'schedule', 'postage_mode', 'status_badge', 'priority', 'is_active')
    list_editable = ('priority', 'is_active')
    list_filter = (OpenWindowStatusFilter, 'postage_mode')
    search_fields = ('title',)
    filter_horizontal = ('provinces',)
    autocomplete_fields = ('cities',)
    formfield_overrides = {models.DateTimeField: {'form_class': JalaliSplitDateTimeField}}
    readonly_fields = ('status_badge', 'created_at', 'updated_at')

    fieldsets = (
        ('قاعده', {'fields': ('title', 'is_active', 'priority', 'status_badge')}),
        ('شرط سبد', {'fields': ('min_cart_total',),
                     'description': 'مبلغ کالاها پس از تخفیف‌های خودکار و کد تخفیفِ کالا. مساوی یا بالاتر از این مبلغ رایگان می‌شود.'}),
        ('بازه‌ی زمانی', {'fields': ('starts_at', 'ends_at'), 'description': 'خالی = بدون محدودیت. برای کمپین‌های مناسبتی تاریخ شروع و پایان بگذارید.'}),
        ('محدوده‌ی جغرافیایی', {'fields': ('provinces', 'cities'),
                                'description': 'هر دو خالی = کل کشور. وگرنه شهرِ آدرس در «شهرها» باشد یا استانش در «استان‌ها».'}),
        ('ارسال با پست', {'fields': ('postage_mode',),
                          'description': 'شهرهای بدون پیک با پس‌کرایه می‌روند. «هزینه‌ی پست با فروشگاه» یعنی برای این شهرها هم ارسال رایگان اعلام شود.'}),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related('provinces', 'cities')

    @admin.display(description='وضعیت')
    def status_badge(self, obj):
        return _status_badge(obj)

    @admin.display(description='حداقل سبد', ordering='min_cart_total')
    def min_total(self, obj):
        return f'{obj.min_cart_total:,} تومان' if obj.min_cart_total else 'بدون حداقل'

    @admin.display(description='محدوده')
    def scope_summary(self, obj):
        provinces = [p.name for p in obj.provinces.all()]
        cities = [c.name for c in obj.cities.all()]
        if not provinces and not cities:
            return 'کل کشور'
        parts = []
        if provinces:
            parts.append('استان: ' + '، '.join(provinces[:3]) + (f' (+{len(provinces) - 3})' if len(provinces) > 3 else ''))
        if cities:
            parts.append('شهر: ' + '، '.join(cities[:3]) + (f' (+{len(cities) - 3})' if len(cities) > 3 else ''))
        return ' | '.join(parts)

    @admin.display(description='بازه (شمسی)')
    def schedule(self, obj):
        return f'{_jalali(obj.starts_at)} ← {_jalali(obj.ends_at)}'
