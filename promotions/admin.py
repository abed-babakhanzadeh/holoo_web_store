"""
ادمین تخفیف‌ها: سیاست سراسری (تک‌ردیفی) و تخفیف‌های خودکار با اهداف (محصول/دسته/برند/کل فروشگاه)، فیلتر وضعیت، عملیات
گروهی (فعال/غیرفعال/تمدید/کپی)، پیش‌نمایش «چند محصول مشمول است» و هشدار هم‌پوشانی با تخفیف‌های دیگر.
"""

from datetime import timedelta

import jdatetime
from django import forms
from django.contrib import admin, messages
from django.db import models, transaction
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html

from products.models import Product
from services.jalali_widgets import JalaliSplitDateTimeField

from .flash import rule_product_filter
from .index import category_children_map, make_rule
from .models import DiscountPolicy, Promotion, PromotionTarget

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
