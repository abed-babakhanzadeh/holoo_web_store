from django.contrib import admin
from .models import Order, OrderItem

class OrderItemInline(admin.TabularInline):
    model = OrderItem
    raw_id_fields = ['product']
    extra = 0

@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'first_name', 'phone', 'payment_method', 'total_price', 'status', 'holoo_preinvoice_id', 'created_at']
    list_filter = ['status', 'payment_method', 'created_at']
    search_fields = ['first_name', 'last_name', 'phone', 'holoo_preinvoice_id']
    inlines = [OrderItemInline]
    readonly_fields = ['created_at', 'updated_at']
    