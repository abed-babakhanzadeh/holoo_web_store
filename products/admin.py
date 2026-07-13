from django.contrib import admin
from .models import Category, Product, Feature, ProductFeatureValue

@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'parent', 'erp_code', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('name', 'erp_code')
    # قابلیت پر شدن خودکار اسلاگ (URL) از روی نام دسته‌بندی
    prepopulated_fields = {'slug': ('name',)}


@admin.register(Feature)
class FeatureAdmin(admin.ModelAdmin):
    list_display = ('name',)
    search_fields = ('name',)


# این کلاس معجزه مدل EAV ما است!
# باعث می‌شود فرم مقداردهی ویژگی‌ها، به صورت یک جدول (Tabular) زیر فرم محصول قرار بگیرد.
class ProductFeatureValueInline(admin.TabularInline):
    model = ProductFeatureValue
    extra = 1 # تعداد ردیف‌های خالی پیش‌فرض برای اضافه کردن ویژگی جدید
    autocomplete_fields = ['feature'] # برای جستجوی راحت‌تر در لیست ویژگی‌ها


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('name', 'category', 'price_formatted', 'stock', 'erp_code', 'is_active')
    list_filter = ('is_active', 'category')
    search_fields = ('name', 'erp_code', 'product_code')
    prepopulated_fields = {'slug': ('name',)}
    
    # فیلدهایی که قرار است توسط تسک سلری از هلو بیایند را برای ادمین Read-Only می‌کنیم
    # تا ادمین به صورت دستی قیمت یا موجودی را دستکاری نکند (فقط هلو صاحب این دیتاست)
    readonly_fields = (
        'price', 'price2', 'price3', 'price4', 'price5', 
        'price6', 'price7', 'price8', 'price9', 'price10', 
        'stock', 'unit', 'created_at', 'updated_at' # unit اضافه شد
    )
    
    # اتصال جدول ویژگی‌ها به فرم اصلی محصول
    inlines = [ProductFeatureValueInline]

    fieldsets = (
        ('اطلاعات پایه سایت', {
            'fields': ('name', 'slug', 'category', 'description', 'main_image', 'is_active')
        }),
        ('اطلاعات مالی و انبار (قفل شده - دریافت از هلو)', {
            'fields': (
                'erp_code', 'product_code', 'stock', 'unit', # unit اضافه شد
                'price', 'price2', 'price3', 'price4', 'price5', 
                'price6', 'price7', 'price8', 'price9', 'price10'
            ),
            'description': 'قیمت‌ها، موجودی و واحد کالا مستقیماً از سیستم هلو خوانده می‌شود.'
        }),
        ('تاریخچه‌ها', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    # نمایش شیک‌تر قیمت با جداکننده هزارگان
    def price_formatted(self, obj):
        return f"{obj.price:,.0f} تومان"
    price_formatted.short_description = 'قیمت فروش'
    