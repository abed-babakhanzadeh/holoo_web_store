from django.contrib import admin
from .models import Order, OrderItem

class OrderItemInline(admin.TabularInline):
    model = OrderItem
    raw_id_fields = ['product']
    extra = 0
    # اسنپ‌شات تخفیف لحظه‌ی ثبت است و با فاکتور هلو هماهنگ؛ ویرایش دستی‌اش مغایرت مالی می‌سازد
    readonly_fields = ['original_price', 'discount_amount']

@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'first_name', 'phone', 'city', 'shipping_method', 'payment_method', 'total_price', 'status', 'tracking_code', 'is_paid', 'holoo_invoice_id', 'holoo_sync_alert_sent', 'created_at']
    list_filter = ['status', 'payment_method', 'shipping_method', 'holoo_sync_alert_sent', 'created_at']
    search_fields = ['first_name', 'last_name', 'phone', 'holoo_invoice_id', 'city', 'province']
    inlines = [OrderItemInline]

    # اسنپ‌شات مقصد و روش ارسال در لحظه‌ی ثبت سفارش گرفته می‌شود و همان فاکتورِ ثبت‌شده است (در هلو هم همین رفته)؛
    # اپراتور نباید تاریخچه‌ی آن را دستکاری کند. اصلاح تایپیِ خودِ متن آدرس/گیرنده با فیلدهای عادی ممکن است.
    # مبلغ‌ها (کرایه و جمع کل) هم فقط‌خواندنی‌اند: با تراکنش بانکی و فاکتور هلو هماهنگ‌اند و تغییر دستی‌شان
    # مغایرت مالی می‌سازد.
    readonly_fields = ['created_at', 'updated_at', 'province', 'city', 'zone', 'full_address_display',
                       'shipping_method', 'shipping_label', 'shipping_cost', 'total_price',
                       'promotion_discount', 'order_discount', 'order_discount_label']

    fieldsets = (
        (None, {'fields': ('user', 'status', 'tracking_code', 'payment_method', 'total_price', 'shipping_cost')}),
        ('تخفیف (اسنپ‌شات لحظه‌ی ثبت؛ غیرقابل ویرایش)', {
            'fields': ('promotion_discount', 'order_discount', 'order_discount_label'),
        }),
        ('گیرنده', {'fields': ('first_name', 'last_name', 'phone', 'postal_code', 'address')}),
        ('مقصد و روش ارسال (اسنپ‌شات لحظه‌ی ثبت؛ غیرقابل ویرایش)', {
            'fields': ('province', 'city', 'zone', 'full_address_display', 'shipping_method', 'shipping_label'),
        }),
        ('حسابداری هلو', {'fields': ('holoo_invoice_id', 'holoo_receipt_id', 'holoo_sync_alert_sent')}),
        ('زمان‌ها', {'fields': ('created_at', 'updated_at')}),
    )

    @admin.display(description='آدرس کامل')
    def full_address_display(self, obj):
        return obj.full_address or '-'

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
