from django.contrib import admin
from django.db.models import Count

from .models import City, DeliveryZone, Province


class DeliveryZoneInline(admin.TabularInline):
    model = DeliveryZone
    extra = 0
    fields = ('name', 'shipping_cost', 'is_active', 'sort_order')


@admin.register(Province)
class ProvinceAdmin(admin.ModelAdmin):
    list_display = ('name', 'city_count', 'sort_order')
    list_editable = ('sort_order',)
    search_fields = ('name',)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_city_count=Count('cities'))

    @admin.display(description='تعداد شهرها', ordering='_city_count')
    def city_count(self, obj):
        return obj._city_count


@admin.register(City)
class CityAdmin(admin.ModelAdmin):
    list_display = ('name', 'province', 'zone_count', 'is_active')
    list_filter = ('province', 'is_active')
    list_editable = ('is_active',)
    list_select_related = ('province',)
    search_fields = ('name', 'province__name')
    autocomplete_fields = ('province',)
    inlines = (DeliveryZoneInline,)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_zone_count=Count('zones'))

    @admin.display(description='تعداد نواحی', ordering='_zone_count')
    def zone_count(self, obj):
        return obj._zone_count


@admin.register(DeliveryZone)
class DeliveryZoneAdmin(admin.ModelAdmin):
    list_display = ('name', 'city', 'tariff', 'shipping_cost', 'is_active', 'sort_order')
    list_editable = ('shipping_cost', 'is_active', 'sort_order')
    list_filter = ('is_active', 'city__province')
    list_select_related = ('city', 'city__province')
    search_fields = ('name', 'city__name')
    autocomplete_fields = ('city',)

    @admin.display(description='وضعیت تعرفه')
    def tariff(self, obj):
        return 'ثبت‌شده' if obj.has_tariff else 'تعرفه تنظیم‌نشده'
