from django import forms
from django.contrib import admin, messages
from django.shortcuts import redirect
from django.urls import path, reverse
from django.utils.safestring import mark_safe
from .models import Category, Product, Feature, ProductFeatureValue, Brand, ProductImage, ProductColor
from .services import sync_product_images


class ColorPickerWidget(forms.TextInput):
    """
    کنار فیلد متنی کد رنگ (Hex)، یک انتخابگر رنگ بصری نمایش می‌دهد تا لازم نباشد کد رنگ را از قبل بلد باشید.
    هماهنگی بین این دو با یک اسکریپت مبتنی بر event delegation (نه id) انجام می‌شود تا برای ردیف‌های
    تازه اضافه‌شده با «افزودن یکی دیگر» هم درست کار کند؛ علاوه بر آن، درست قبل از ارسال فرم، مقدار
    انتخابگر به‌عنوان مرجع نهایی در فیلد متنی نشانده می‌شود تا خطای «این فیلد الزامی است» رخ ندهد.
    """

    class Media:
        js = ('products/admin/color_picker_sync.js',)

    def render(self, name, value, attrs=None, renderer=None):
        attrs = dict(attrs or {})
        attrs['class'] = (attrs.get('class', '') + ' color-hex-input').strip()
        text_html = super().render(name, value, attrs, renderer)

        safe_value = value if (value and len(value) == 7) else '#000000'
        picker_html = (
            '<input type="color" class="color-hex-picker" value="%s" '
            'style="width:36px;height:30px;padding:0;border:1px solid #ccc;border-radius:4px;'
            'vertical-align:middle;margin-inline-start:6px;cursor:pointer;">'
        ) % safe_value

        return mark_safe(f'<span class="color-picker-wrap">{text_html}{picker_html}</span>')

@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'parent', 'erp_code', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('name', 'erp_code')
    # قابلیت پر شدن خودکار اسلاگ (URL) از روی نام دسته‌بندی
    prepopulated_fields = {'slug': ('name',)}


@admin.register(Brand)
class BrandAdmin(admin.ModelAdmin):
    list_display = ('name', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('name',)
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


class ProductImageInline(admin.TabularInline):
    """ نمایش صرفاً خواندنیِ گالری تصاویر؛ تنها راه تغییرِ تصاویر محصول، پوشه‌ی کاتالوگ + دکمه‌ی سینک است """
    model = ProductImage
    extra = 0
    readonly_fields = ('image', 'order')
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class ProductColorForm(forms.ModelForm):
    class Meta:
        model = ProductColor
        fields = '__all__'
        widgets = {
            'hex_code': ColorPickerWidget(),
        }


class ProductColorInline(admin.TabularInline):
    model = ProductColor
    form = ProductColorForm
    extra = 1


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('name', 'category', 'brand', 'price_formatted', 'stock', 'erp_code', 'is_active')
    list_filter = ('is_active', 'category', 'brand')
    search_fields = ('name', 'erp_code', 'product_code')
    prepopulated_fields = {'slug': ('name',)}
    autocomplete_fields = ['brand']

    def get_urls(self):
        urls = [
            path('sync-images/', self.admin_site.admin_view(self.sync_images_view), name='products_product_sync_images'),
        ]
        return urls + super().get_urls()

    def sync_images_view(self, request):
        """
        دکمه‌ی «همگام‌سازی تصاویر»: پوشه‌ی media/products/catalog/ را می‌خواند و بر اساس نام فایل
        (erp_code-شماره.jpg) عکس اصلی/گالری محصولات را وصل می‌کند - بدون نیاز به آپلود دستی.
        """
        result = sync_product_images()

        messages.success(
            request,
            f"همگام‌سازی تصاویر انجام شد. تطبیق‌یافته: {result['matched']} | "
            f"به‌روزشده: {result['updated']} | حذف‌شده (فایل دیگر نبود): {result['pruned']}"
        )
        if result['unmatched']:
            messages.warning(
                request,
                f"{len(result['unmatched'])} فایل به هیچ محصولی متصل نشد (کد کالا اشتباه یا نامعتبر بود): "
                + '، '.join(result['unmatched'])
            )

        return redirect(reverse('admin:products_product_changelist'))

    # فیلدهایی که قرار است توسط تسک سلری از هلو بیایند را برای ادمین Read-Only می‌کنیم
    # تا ادمین به صورت دستی قیمت یا موجودی را دستکاری نکند (فقط هلو صاحب این دیتاست)
    # main_image هم Read-Only است: تنها راه تغییر تصاویر محصول، پوشه‌ی کاتالوگ + دکمه‌ی سینک است
    readonly_fields = (
        'price', 'price2', 'price3', 'price4', 'price5',
        'price6', 'price7', 'price8', 'price9', 'price10',
        'stock', 'unit', 'created_at', 'updated_at', # unit اضافه شد
        'main_image',
    )

    # اتصال جدول ویژگی‌ها، گالری تصاویر و رنگ‌بندی به فرم اصلی محصول
    inlines = [ProductImageInline, ProductColorInline, ProductFeatureValueInline]

    fieldsets = (
        ('اطلاعات پایه سایت', {
            'fields': ('name', 'slug', 'category', 'brand', 'warranty', 'description', 'main_image', 'is_active'),
            'description': 'تصویر اصلی و گالری محصول قفل هستند؛ برای تغییرشان فایل را با نام «کد کالا-شماره.jpg» در پوشه‌ی کاتالوگ بگذارید و دکمه‌ی «همگام‌سازی تصاویر از پوشه» را در لیست محصولات بزنید.'
        }),
        ('توضیحات تکمیلی', {
            'fields': ('additional_description',),
            'description': 'می‌توانید مثل یک ویرایشگر متنی معمولی، متن را قالب‌بندی کنید و در هر جای دلخواه عکس اضافه کنید.'
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
    