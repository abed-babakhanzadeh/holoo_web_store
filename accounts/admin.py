from django.contrib import admin
from django.utils.html import format_html
from django.utils import timezone
from .models import CustomUser, OTPRequest

@admin.register(CustomUser)
class CustomUserAdmin(admin.ModelAdmin):
    # اضافه شدن نام و نام خانوادگی به لیست اصلی
    list_display = ('phone_number', 'get_full_name', 'colored_status', 'erp_code', 'date_joined', 'is_active')
    
    list_filter = ('status', 'is_active', 'is_staff', 'date_joined', 'state') # فیلتر بر اساس استان هم اضافه شد
    
    # اضافه شدن کد ملی و نام به باکس جستجو
    search_fields = ('phone_number', 'erp_code', 'national_code', 'first_name', 'last_name')
    
    ordering = ('-date_joined',)
    
    readonly_fields = ('date_joined', 'last_login', 'retry_count', 'last_sync_error')
    
    # دسته‌بندی جدید و بسیار مرتب فیلدها در صفحه ویرایش
    fieldsets = (
        ('اطلاعات ورود و پایه', {
            'fields': ('phone_number', 'status', 'erp_code')
        }),
        ('اطلاعات هویتی', {
            'fields': ('first_name', 'last_name', 'national_code')
        }),
        ('موقعیت و آدرس', {
            'fields': ('state', 'city', 'postal_code', 'address')
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
    colored_status.short_description = 'وضعیت کاربر'

    def get_full_name(self, obj):
        name = f"{obj.first_name or ''} {obj.last_name or ''}".strip()
        return name if name else "-"
    get_full_name.short_description = 'نام و نام خانوادگی'


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