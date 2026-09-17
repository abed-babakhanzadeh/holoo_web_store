from django.contrib import admin
from .models import Order, OrderItem

class OrderItemInline(admin.TabularInline):
    model = OrderItem
    raw_id_fields = ['product']
    extra = 0

@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'first_name', 'phone', 'payment_method', 'total_price', 'status', 'tracking_code', 'is_paid', 'holoo_invoice_id', 'holoo_sync_alert_sent', 'created_at']
    list_filter = ['status', 'payment_method', 'holoo_sync_alert_sent', 'created_at']
    search_fields = ['first_name', 'last_name', 'phone', 'holoo_invoice_id']
    inlines = [OrderItemInline]
    readonly_fields = ['created_at', 'updated_at']

    def save_model(self, request, obj, form, change):
        """
        وقتی ادمین در همین ذخیره «وضعیت» یا «کد رهگیری» را عوض کند و بعد از ذخیره سفارش
        هم status='shipped' باشد هم tracking_code پر باشد، پیامک کد رهگیری برای مشتری
        می‌رود. شرط «در همین ذخیره تغییر کرده» (form.changed_data) از ارسال تکراری در
        ذخیره‌های بعدی که به این دو فیلد کاری ندارند جلوگیری می‌کند.
        """
        super().save_model(request, obj, form, change)

        shipped_now = obj.status == 'shipped' and obj.tracking_code and obj.user and (
            'status' in form.changed_data or 'tracking_code' in form.changed_data
        )
        if shipped_now:
            from notifications.service import notify
            notify(
                obj.user.phone_number, 'order_shipped_customer',
                name=obj.user.first_name or '', tracking_code=obj.tracking_code,
            )
