from django.contrib import admin
from django.utils.html import format_html
from django.utils import timezone
from .models import CustomUser, OTPRequest

@admin.register(CustomUser)
class CustomUserAdmin(admin.ModelAdmin):
    # فیلدهایی که در لیست اصلی کاربران نمایش داده می‌شوند
    list_display = ('phone_number', 'colored_status', 'erp_code', 'retry_count', 'date_joined', 'is_active')
    
    # فیلدهایی که سمت راست برای فیلتر سریع ظاهر می‌شوند
    list_filter = ('status', 'is_active', 'is_staff', 'date_joined')
    
    # باکس جستجوی قدرتمند
    search_fields = ('phone_number', 'erp_code', 'last_sync_error')
    
    # مرتب‌سازی پیش‌فرض (جدیدترین کاربران اول بیایند)
    ordering = ('-date_joined',)
    
    # فیلدهایی که داخل صفحه ویرایش کاربر فقط خواندنی (Read-Only) هستند تا ادمین دستی خرابکاری نکند
    readonly_fields = ('date_joined', 'last_login', 'retry_count', 'last_sync_error')
    
    # راست‌چین کردن و مرتب‌سازی فیلدها در صفحه ویرایش کاربر (دسته بندی فیلدها)
    fieldsets = (
        ('اطلاعات پایه', {'fields': ('phone_number', 'status', 'erp_code')}),
        ('وضعیت یکپارچه‌سازی هلو', {'fields': ('retry_count', 'last_sync_error')}),
        ('دسترسی‌ها و تاریخ‌ها', {'fields': ('is_active', 'is_staff', 'is_superuser', 'date_joined', 'last_login')}),
    )

    # نمایش وضعیت‌ها به صورت رنگی و شیک در لیست
    def colored_status(self, obj):
        colors = {
            'PENDING': '#ff9800',     # نارنجی
            'PROCESSING': '#2196f3',  # آبی
            'APPROVED': '#4caf50',    # سبز
            'REJECTED': '#f44336',    # قرمز
        }
        color = colors.get(obj.status, '#000000')
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 10px; border-radius: 12px; font-weight: bold; font-size: 11px;">{}</span>',
            color,
            obj.get_status_display()
        )
    colored_status.short_description = 'وضعیت کاربر'


@admin.register(OTPRequest)
class OTPRequestAdmin(admin.ModelAdmin):
    list_display = ('phone_number', 'code', 'purpose', 'attempt_count', 'is_expired', 'used_status')
    list_filter = ('purpose', 'created_at')
    search_fields = ('phone_number', 'code')
    ordering = ('-created_at',)
    
    # تعریف فیلدهای فقط خواندنی برای امنیت دیتای OTP
    readonly_fields = ('phone_number', 'code', 'purpose', 'ip_address', 'attempt_count', 'created_at', 'expires_at', 'used_at')

    # وضعیت انقضا را به صورت آیکون یا متن شیک نشان دهد
    def is_expired(self, obj):
        expired = timezone.now() > obj.expires_at
        if expired and not obj.used_at:
            return format_html('<span style="color: #f44336;">منقضی شده</span>')
        elif obj.used_at:
            return format_html('<span style="color: #9e9e9e;">-</span>')
        return format_html('<span style="color: #4caf50;">معتبر</span>')
    is_expired.short_description = 'وضعیت زمان'

    # وضعیت استفاده شدن کد
    def used_status(self, obj):
        if obj.used_at:
            return format_html('<span style="color: #4caf50;">استفاده شده در {}</span>', obj.used_at.strftime('%H:%M'))
        return format_html('<span style="color: #ff9800;">استفاده نشده</span>')
    used_status.short_description = 'وضعیت مصرف'