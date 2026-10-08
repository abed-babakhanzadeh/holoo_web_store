from django import forms
from django.contrib import admin, messages
from django.db import models
from django.shortcuts import redirect
from django.urls import path, reverse
from django.utils.safestring import mark_safe
from django.utils import timezone
from .models import (
    Category, CategoryBanner, Product, Feature, ProductFeatureValue,
    Brand, Warranty, ProductImage, ProductColor, SiteSettings, StockAlert, Story,
    HomeBanner, HeroSlide, NewsletterSubscriber, ContactMessage,
)
from .services import sync_product_images
from services.jalali_widgets import JalaliSplitDateTimeField


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

class CategoryBannerInline(admin.TabularInline):
    model = CategoryBanner
    extra = 0
    max_num = 5
    fields = ('image', 'link_product', 'link_url', 'order')
    autocomplete_fields = ['link_product']


class TopLevelCategoryFilter(admin.SimpleListFilter):
    """ فیلتر «دسته اصلی» در لیست دسته‌بندی‌ها؛ چون سطح بودن یک فیلد واقعی نیست (parent__isnull است) """
    title = 'دسته اصلی'
    parameter_name = 'top_level'

    def lookups(self, request, model_admin):
        return (('1', 'فقط دسته‌های اصلی'), ('0', 'فقط زیردسته‌ها'))

    def queryset(self, request, queryset):
        if self.value() == '1':
            return queryset.filter(parent__isnull=True)
        if self.value() == '0':
            return queryset.filter(parent__isnull=False)
        return queryset


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'parent', 'erp_code', 'is_active')
    list_filter = ('is_active', TopLevelCategoryFilter)
    search_fields = ('name', 'erp_code')
    # قابلیت پر شدن خودکار اسلاگ (URL) از روی نام دسته‌بندی
    prepopulated_fields = {'slug': ('name',)}
    autocomplete_fields = ['parent']
    filter_horizontal = ('suggested_categories', 'related_blog_categories')
    inlines = [CategoryBannerInline]

    def get_fieldsets(self, request, obj=None):
        # تصویر شاخص برای همه (اصلی/زیردسته) لازم است؛ بقیه‌ی تنظیمات صفحه‌ی اختصاصی فقط
        # وقتی معنا دارند که دسته والد نداشته باشد (obj=None یعنی هنوز در فرم افزودن هستیم
        # و والد مشخص نشده، پس فعلاً نشان داده می‌شود)
        fieldsets = [
            (None, {'fields': ('name', 'slug', 'parent', 'erp_code', 'is_active', 'featured_image')}),
        ]
        if obj is None or obj.parent_id is None:
            fieldsets.append((
                'صفحه‌ی اختصاصی دسته (فقط دسته‌های اصلی)',
                {'fields': (
                    'short_description', 'suggested_categories', 'related_blog_categories',
                    'show_amazing_deals', 'show_suggested_categories', 'show_best_sellers',
                    'show_frequent', 'show_banners', 'show_blog_posts',
                )},
            ))
            fieldsets.append((
                'بنر مگامنوی هدر (فقط دسته‌های اصلی)',
                {
                    'description': 'در ستون کناری مگامنو، هنگام هاور روی این دسته نمایش داده می‌شود. نمایش/عدم‌نمایش '
                                   'کلی بنرها و عرض ستون بنر از «تنظیمات سایت ← مگامنوی دسته‌بندی‌ها» است.',
                    'fields': ('mega_menu_banner', 'mega_menu_banner_url', 'mega_menu_banner_alt'),
                },
            ))
        return fieldsets


@admin.register(Brand)
class BrandAdmin(admin.ModelAdmin):
    list_display = ('name', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('name',)
    prepopulated_fields = {'slug': ('name',)}


@admin.register(Story)
class StoryAdmin(admin.ModelAdmin):
    """
    مدیریت کامل استوری‌های صفحه اصلی: ترتیب نمایش و فعال/غیرفعال بودن مستقیم از
    صفحه‌ی لیست قابل تغییرند (list_editable)، بدون باز کردن هر رکورد؛ سوییچ سراسری
    «نمایش/عدم‌نمایش کل بخش» در تنظیمات سایت است (SiteSettingsAdmin).
    """
    list_display = ('cover_thumb', 'title', 'story_type', 'order', 'is_active', 'starts_at', 'ends_at', 'created_at')
    list_display_links = ('cover_thumb', 'title')
    list_editable = ('order', 'is_active')
    list_filter = ('story_type', 'is_active')
    search_fields = ('title',)
    autocomplete_fields = ['link_product']
    formfield_overrides = {
        models.DateTimeField: {'form_class': JalaliSplitDateTimeField},
    }
    fieldsets = (
        ('نوع و عنوان', {'fields': ('story_type', 'title')}),
        ('رسانه', {'fields': ('cover_image', 'image', 'video', 'duration_ms')}),
        ('لینک مقصد (اختیاری)', {'fields': ('link_product', 'link_url')}),
        ('زمان‌بندی نمایش (اختیاری)', {'fields': ('starts_at', 'ends_at')}),
        ('نمایش', {'fields': ('order', 'is_active')}),
    )

    @admin.display(description='کاور')
    def cover_thumb(self, obj):
        if obj.cover_image:
            return mark_safe(f'<img src="{obj.cover_image.url}" style="width:40px;height:40px;border-radius:50%;object-fit:cover;">')
        return '—'


@admin.register(Warranty)
class WarrantyAdmin(admin.ModelAdmin):
    list_display = ('name', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('name',)


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
    list_display = ('name', 'category', 'brand', 'price_formatted', 'stock', 'erp_code', 'is_active', 'free_shipping')
    list_filter = ('is_active', 'free_shipping', 'category', 'brand')
    search_fields = ('name', 'erp_code', 'product_code')
    prepopulated_fields = {'slug': ('name',)}
    autocomplete_fields = ['brand', 'warranty']

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
        'stock', 'reserved_quantity', 'stock_synced_at', 'price_synced_at', 'unit', 'created_at', 'updated_at', # unit اضافه شد
        'main_image',
    )

    # اتصال جدول ویژگی‌ها، گالری تصاویر و رنگ‌بندی به فرم اصلی محصول
    inlines = [ProductImageInline, ProductColorInline, ProductFeatureValueInline]

    fieldsets = (
        ('اطلاعات پایه سایت', {
            'fields': ('name', 'slug', 'category', 'brand', 'warranty', 'description', 'main_image', 'is_active', 'free_shipping'),
            'description': 'تصویر اصلی و گالری محصول قفل هستند؛ برای تغییرشان فایل را با نام «کد کالا-شماره.jpg» در پوشه‌ی کاتالوگ بگذارید و دکمه‌ی «همگام‌سازی تصاویر از پوشه» را در لیست محصولات بزنید.'
        }),
        ('توضیحات تکمیلی', {
            'fields': ('additional_description',),
            'description': 'می‌توانید مثل یک ویرایشگر متنی معمولی، متن را قالب‌بندی کنید و در هر جای دلخواه عکس اضافه کنید.'
        }),
        ('اطلاعات مالی و انبار (قفل شده - دریافت از هلو)', {
            'fields': (
                'erp_code', 'product_code', 'stock', 'reserved_quantity', 'stock_synced_at', 'price_synced_at', 'unit', # unit اضافه شد
                'price', 'price2', 'price3', 'price4', 'price5', 
                'price6', 'price7', 'price8', 'price9', 'price10'
            ),
            'description': 'قیمت‌ها، موجودی و واحد کالا مستقیماً از سیستم هلو خوانده می‌شود. «رزروشده» سهم سفارش‌های ثبت‌شده‌ی سایت است '
                           'که هنوز در موجودی هلو دیده نمی‌شود؛ موجودی قابل‌فروش = موجودی − رزروشده − بافر اطمینان.'
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


# گروه‌های تب‌های «تنظیمات سایت» (کلید، عنوان تب) به ترتیب نمایش؛ هر بخش (fieldset) با کلاس
# sgroup-<کلید> به یکی از این‌ها تعلق دارد - نگاه کنید _site_settings_section و
# static/products/admin/site_settings_tabs.js. تست رگرسیون تضمین می‌کند هر بخش گروه معتبر دارد.
SITE_SETTINGS_GROUPS = (
    ('appearance', 'ظاهر و چیدمان'),
    ('megamenu', 'مگامنوی هدر'),
    ('homepage', 'صفحه اصلی'),
    ('store', 'هویت و تماس فروشگاه'),
    ('about', 'صفحه درباره ما'),
    ('contact', 'فوتر، شبکه‌ها و اطلاع‌رسانی'),
    ('sales', 'فروش و ارسال'),
    ('customers', 'مشتریان و پس از فروش'),
    ('chat', 'گفتگوی آنلاین'),
)


def _site_settings_section(group, title, fields, description=''):
    """ یک بخش تاشوی «تنظیمات سایت» که به گروه (تب) group تعلق دارد """
    options = {'classes': ('collapse', f'sgroup-{group}'), 'fields': fields}
    if description:
        options['description'] = description
    return (title, options)


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    """ تنظیمات سایت تک‌ردیفی است؛ لیست همیشه مستقیم به فرم ویرایش همان یک ردیف می‌رود
    و افزودن/حذف ردیف جدید غیرفعال است تا دومین ردیف اشتباهی ساخته نشود """
    # 'classes': ('collapse',) روی همه‌ی بخش‌ها یعنی هر fieldset با تگ بومی <details>/<summary>
    # تاشو رندر می‌شود (نگاه کنید admin/includes/fieldset.html جنگو - از collapse.js قدیمی خبری
    # نیست). هر بخش علاوه بر آن به یک «گروه» تعلق دارد (کلاس sgroup-<کلید>، فهرست گروه‌ها در
    # SITE_SETTINGS_GROUPS): اسکریپت اختصاصی site_settings_tabs.js (پایین در Media) بالای فرم یک
    # نوار تب می‌سازد و هر بار فقط بخش‌های یک گروه را نشان می‌دهد؛ داخل هر گروه هم «فقط یکی همیشه باز» و
    # انیمیشن نرم باز/بسته‌شدن دارد. بدون JS همه‌ی بخش‌ها مثل قبل زیر هم دیده می‌شوند و فرم سالم می‌ماند.
    fieldsets = (
        # ---- ظاهر و چیدمان ----
        _site_settings_section('appearance', 'برندسازی (لوگو، فاوآیکون، بنر صفحه‌ی ورود)', (
            'logo_image', 'favicon_image', 'login_hero_image', 'login_badge_icon',
            'login_banner_title', 'login_banner_subtitle',
        ), 'هرکدام خالی بماند، همان فایل/متن پیش‌فرض تم استفاده می‌شود.'),
        _site_settings_section('appearance', 'چیدمان ظاهری فروشگاه', (
            'site_content_max_width', 'default_shop_columns', 'hero_slider_width_mode',
        ), 'عرض محتوا روی صفحه‌نمایش‌های عریض، چیدمان پیش‌فرض کارت‌های محصول و عرض اسلایدر اصلی '
           'در صفحه‌ی فروشگاه. این تنظیمات روی پنل کاربری اثر ندارند.'),
        _site_settings_section('appearance', 'افکت سبد خرید', ('cart_fly_animation_enabled', 'cart_hover_popup_enabled', 'cart_fly_respect_reduced_motion'),
                               'انیمیشن افزودن به سبد (پرواز عکس محصول به آیکون سبد و پنجره‌ی کوچک سبد). در موبایل به‌صورت خودکار '
                               'به آیکون سبد منوی پایین می‌رود. کاربرانی که حرکت کم را در سیستم‌شان فعال کرده‌اند فقط پنجره‌ی سبد را می‌بینند.'),
        _site_settings_section('appearance', 'چیدمان ظاهری پنل کاربری', ('dashboard_content_max_width',),
                               'عرض صفحات پنل کاربری (حساب کاربری، سفارش‌ها، باشگاه مشتریان و ...)؛ مستقل از عرض '
                               'صفحات فروشگاهی.'),
        _site_settings_section('appearance', 'چیدمان صفحه‌ی مدیریت جنگو (/admin)', ('admin_panel_max_width',),
                               'خالی بماند، صفحات /admin/ همان رفتار پیش‌فرض جنگو (تمام عرض) را دارند؛ مستقل از عرض '
                               'سایت و عرض پنل کاربری.'),
        _site_settings_section('appearance', 'تصاویر پیش‌فرض محصول بدون عکس', ('no_image_1', 'no_image_2'),
                               'برای محصولاتی که هنوز از اسکنر عکس نگرفته‌اند. هرکدام خالی بماند، همان فایل پیش‌فرض تم '
                               '(Preload.webp / Preload-2.webp) استفاده می‌شود.'),

        # ---- مگامنوی هدر ----
        _site_settings_section('megamenu', 'ابعاد و چیدمان', (
            'mega_menu_width_mode', 'mega_menu_width_value', 'mega_menu_max_height', 'mega_menu_columns',
            'mega_menu_show_banner', 'mega_menu_banner_width',
        ), 'مگامنوی دسکتاپ هدر. بنر هر دسته‌ی اصلی در صفحه‌ی ویرایش همان دسته تعریف می‌شود.'),
        _site_settings_section('megamenu', 'پس‌زمینه و افکت شیشه‌ای', (
            'mega_menu_bg_color', 'mega_menu_bg_opacity', 'mega_menu_blur_px',
            'mega_menu_bg_image', 'mega_menu_bg_image_mode',
        ), 'فقط روی تم روشن اعمال می‌شود؛ تم تیره همیشه استایل پیش‌فرض خودش را دارد. '
           'افکت شیشه‌ای = شفافیت کمتر از ۱۰۰ + بلور.'),
        _site_settings_section('megamenu', 'تصاویر دسته‌ها', (
            'mega_menu_show_parent_images', 'mega_menu_show_child_images', 'mega_menu_image_position',
            'mega_menu_image_size', 'mega_menu_image_gap',
        )),

        # ---- گفتگوی آنلاین ----
        _site_settings_section('chat', 'کلیدها و زبانه‌ها', (
            'chat_enabled', 'chat_tab_live_enabled', 'chat_tab_offline_enabled', 'chat_ai_tab_mode',
            'chat_visible_for_guests', 'chat_visible_for_users',
        ), 'کلید اصلی کل ویجت و روشن/خاموش هر زبانه. زبانه‌ی چت هوشمند فعلاً فقط «به‌زودی» یا مخفی است.'),
        _site_settings_section('chat', 'موقعیت دکمه و صفحات مستثنی', (
            'chat_position', 'chat_offset_x_px', 'chat_offset_y_px', 'chat_offset_x_px_mobile', 'chat_offset_y_px_mobile',
            'chat_excluded_paths',
        ), 'در موبایل اگر فاصله‌ها خالی بماند دکمه خودکار بالای منوی پایین می‌نشیند.'),
        _site_settings_section('chat', 'آواتار و رنگ', ('chat_avatar_choice', 'chat_avatar_custom', 'chat_primary_color')),
        _site_settings_section('chat', 'متن‌ها', (
            'chat_title', 'chat_subtitle_online', 'chat_subtitle_offline', 'chat_tab_live_label', 'chat_tab_offline_label',
            'chat_tab_ai_label', 'chat_welcome_message', 'chat_msg_no_operator', 'chat_msg_after_hours', 'chat_offline_form_intro',
            'chat_offline_success_message', 'chat_ai_coming_soon_text', 'chat_privacy_notice', 'chat_message_placeholder',
            'chat_name_placeholder', 'chat_phone_placeholder', 'chat_send_label', 'chat_to_offline_label', 'chat_close_conversation_label',
            'chat_live_form_intro', 'chat_live_waiting_text', 'chat_typing_text', 'chat_blocked_message',
        )),
        _site_settings_section('chat', 'انیمیشن‌ها و حباب', (
            'chat_anim_enabled', 'chat_anim_float', 'chat_anim_pulse', 'chat_anim_wave', 'chat_anim_bubble',
            'chat_bubble_messages', 'chat_bubble_interval_seconds', 'chat_bubble_first_delay_seconds',
            'chat_attention_interval_seconds', 'chat_respect_reduced_motion', 'chat_launcher_dismiss_hours',
        )),
        _site_settings_section('chat', 'ساعات کاری و تعطیلات', (
            'chat_timezone', 'chat_hours_mode', 'chat_hours_sat', 'chat_hours_sun', 'chat_hours_mon', 'chat_hours_tue',
            'chat_hours_wed', 'chat_hours_thu', 'chat_hours_fri', 'chat_holidays',
        ), 'وضعیت ۱: ساعت کاری + کارشناس آنلاین ← گفتگوی زنده. وضعیت ۲: ساعت کاری ولی کارشناس آنلاین نیست ← فرم آفلاین. '
           'وضعیت ۳: خارج از ساعت کاری ← فرم آفلاین.'),
        _site_settings_section('chat', 'فرم مهمان و محدودیت‌ها (فاز ۲ به بعد)', (
            'chat_guest_name_mode', 'chat_guest_phone_mode', 'chat_message_max_length', 'chat_rate_limit_per_minute',
            'chat_guest_max_conversations_per_day', 'chat_captcha_after_n_conversations', 'chat_retention_days',
        )),
        _site_settings_section('chat', 'پیوست‌ها (فاز ۴)', (
            'chat_attachments_enabled', 'chat_attachments_mode', 'chat_attachment_max_mb', 'chat_attachment_max_count',
        ), 'پیش‌فرض خاموش. هر فایل با بررسی محتوای واقعی (نه فقط پسوند) و در پوشه‌ی جداگانه ذخیره می‌شود.'),
        _site_settings_section('chat', 'حضور کارشناس و به‌روزرسانی (فاز ۳)', (
            'chat_operator_timeout_seconds', 'chat_live_requires_operator', 'chat_typing_indicator_enabled',
            'chat_poll_active_seconds', 'chat_poll_idle_seconds', 'chat_poll_closed_seconds',
        ), 'کارشناس در پیشخوان خود را «آنلاین» اعلام می‌کند؛ اگر نبضش از مهلت بالا قدیمی‌تر شود آنلاین حساب نمی‌شود.'),
        _site_settings_section('chat', 'تایمرهای چرخه‌ی گفتگو (فاز ۳)', (
            'chat_operator_response_sla_minutes', 'chat_customer_idle_minutes', 'chat_customer_gone_minutes',
            'chat_continuity_minutes', 'chat_assignee_timeout_minutes', 'chat_idle_close_hours', 'chat_reopen_window_hours',
            'chat_reopen_on_customer_message',
        )),
        _site_settings_section('chat', 'پیامک گفتگو (فاز ۲)', (
            'chat_admin_sms_cooldown_minutes', 'chat_customer_sms_cooldown_minutes', 'chat_notify_phones',
        )),

        # ---- صفحه اصلی ----
        _site_settings_section('homepage', 'نمایش بخش‌های صفحه اصلی', (
            'show_stories', 'show_hero_slider', 'show_amazing_deal', 'show_best_selling', 'show_blog_posts',
        )),
        _site_settings_section('homepage', 'خبرنامه', ('show_newsletter',)),
        _site_settings_section('homepage', 'دانلود اپلیکیشن', (
            'show_app_download', 'app_google_play_url', 'app_sibapp_url',
            'app_bazaar_url', 'app_myket_url', 'app_direct_download_url',
        )),

        # ---- هویت و تماس فروشگاه (مرجع واحد: هدر، فوتر، «درباره ما»، «تماس با ما»، اسناد و اعلان‌ها) ----
        _site_settings_section('store', 'اطلاعات پایه و حقوقی', (
            'store_name', 'store_legal_name', 'store_national_id', 'store_registration_number',
            'store_economic_code', 'store_postal_code', 'store_address',
        ), 'نام تجاری در همه‌ی قالب‌ها (هدر، فوتر، عنوان صفحه‌ها) و مشخصات ثبتی برای اسناد رسمی و فاکتور خوانده می‌شود. '
           'ارقام فارسی خودکار به لاتین تبدیل می‌شوند.'),
        _site_settings_section('store', 'فاکتور رسمی و مهر فروشگاه', (
            'store_stamp_image', 'invoice_show_legal_name', 'invoice_show_national_id',
            'invoice_show_registration_number', 'invoice_show_economic_code', 'invoice_show_stamp',
        ), 'مشخصات فروشنده‌ی فاکتور از بخش «اطلاعات پایه و حقوقی» خوانده می‌شود. هر مورد فقط وقتی در فاکتور چاپ می‌شود که '
           'سوییچ آن روشن و مقدارش پر باشد. فاکتور ستون مالیات ندارد.'),
        _site_settings_section('store', 'تماس با فروشگاه', (
            'store_phone_1', 'store_phone_2', 'store_mobile', 'store_email_1', 'store_email_2',
            'store_working_hours', 'store_admin_sms_recipient', 'store_admin_sms_recipient_2',
        ), 'در فوتر سایت و صفحه‌ی «تماس با ما» نمایش داده می‌شود. «شماره موبایل مدیر» فقط برای دریافت پیامک‌های '
           'سیستمی (مثل پیام جدید تماس با ما) است و برای مشتری نمایش داده نمی‌شود.'),
        _site_settings_section('store', 'نقشه (صفحه‌ی تماس با ما)', (
            'map_type', 'map_latitude', 'map_longitude', 'map_neshan_url', 'map_iframe_code',
        ), 'مختصات را از گوگل‌مپ یا نشان کپی کنید. برای نوع «iframe دلخواه» کد Embed نقشه را بچسبانید.'),

        # ---- صفحه درباره ما ----
        _site_settings_section('about', 'داستان ما', (
            'about_story_title', 'about_story_text', 'about_story_image',
        ), 'بخش‌های خالی در صفحه‌ی «درباره ما» نمایش داده نمی‌شوند.'),
        _site_settings_section('about', 'مأموریت و ارزش‌ها (۳ کارت)', (
            'about_value1_title', 'about_value1_icon', 'about_value1_text',
            'about_value2_title', 'about_value2_icon', 'about_value2_text',
            'about_value3_title', 'about_value3_icon', 'about_value3_text',
        )),
        _site_settings_section('about', 'آمار کلیدی (۴ مورد)', (
            'about_stat1_value', 'about_stat1_label', 'about_stat2_value', 'about_stat2_label',
            'about_stat3_value', 'about_stat3_label', 'about_stat4_value', 'about_stat4_label',
        ), 'فقط آماری نمایش داده می‌شود که هم عدد شاخص و هم عنوان داشته باشد.'),

        # ---- فوتر، شبکه‌ها و اطلاع‌رسانی ----
        _site_settings_section('contact', 'متن فوتر', ('footer_about_title', 'footer_about_text', 'copyright_text')),
        _site_settings_section('contact', 'نمادهای اعتماد', ('enamad_link', 'trust_seal_link', 'samandehi_link')),
        _site_settings_section('contact', 'شبکه‌های اجتماعی', (
            'rubika_url', 'aparat_url', 'bale_url', 'eitaa_url', 'igap_url', 'soroush_url',
            'instagram_url', 'telegram_url',
        )),
        _site_settings_section('contact', 'اطلاع‌رسانی', ('notification_backend',)),

        # ---- فروش و ارسال ----
        _site_settings_section('sales', 'قیمت برای کاربران مهمان', (
            'guest_pricing_mode', 'guest_price_level', 'guest_adjustment_type', 'guest_adjustment_value',
            'guest_price_rounding_step', 'guest_price_hidden_message',
        ), 'تعیین می‌کند کاربر لاگین‌نکرده چه قیمتی ببیند (مهمان سبد خرید و سفارش ندارد). پیش‌فرض: قیمت سطح ۱ (چکی) '
           '— همان رفتار قبلی. ترتیب محاسبه: سطح پایه ← تعدیل (فقط «قیمت فرمولی») ← تخفیف‌های خودکار. '
           'سطح‌ها: ۱ چکی، ۲ نقدی، ۳ تا ۱۰ ویژه.'),
        _site_settings_section('sales', 'خرید چکی مشتریان ویژه (VIP)', ('vip_cheque_policy',),
                               'مشتری چکی (سطح ۱) همیشه گزینه‌ی چکی و نقدی را می‌بیند؛ مشتری نقدی (سطح ۲) فقط نقدی و گزینه‌ی «درخواست '
                               'خرید چکی» را (مگر مجوز فردی داشته باشد). این تنظیم فقط رفتار مشتری ویژه (سطح ۳ تا ۱۰) را تعیین می‌کند؛ '
                               'مجوز فردی هر کاربر (در صفحه‌ی کاربر) همیشه بر آن اولویت دارد.'),
        _site_settings_section('sales', 'مهلت ثبت اطلاعات چک', ('cheque_submission_deadline_hours',),
                               'پس از ثبت سفارش چکی، مشتری این مدت فرصت دارد دست‌کم یک چک ثبت کند؛ در غیر این صورت سفارش خودکار لغو '
                               'و موجودی رزروشده آزاد می‌شود (بررسی هر ۱۵ دقیقه). با ثبت اولین چک مهلت متوقف می‌شود.'),
        _site_settings_section('sales', 'یکپارچه‌سازی هلو (کرایه حمل و تسویه)', ('shipping_erp_code', 'holoo_pos_sarfasl')),
        _site_settings_section('sales', 'موجودی و رزرو', ('stock_safety_buffer',),
                               'مهلت رزرو سفارش پرداخت‌نشده ۲۰ دقیقه است؛ سفارش پرداخت‌شده یا چکی تا «تأیید سفارش» توسط مدیر '
                               'رزرو می‌ماند.'),
        _site_settings_section('sales', 'سیاست هزینه‌ی حمل', (
            'courier_free_for_free_shipping_cart', 'postage_collect_enabled',
            'postage_collect_label', 'postage_disabled_message',
        ), 'کرایه‌ی پیک هر ناحیه از منوی «نواحی ارسال» تنظیم می‌شود.'),

        # ---- مشتریان و پس از فروش ----
        _site_settings_section('customers', 'امتیاز و سطح مشتریان', (
            'loyalty_mode', 'loyalty_points_per_order', 'loyalty_amount_step',
            'loyalty_threshold_bronze', 'loyalty_threshold_silver',
            'loyalty_threshold_gold', 'loyalty_threshold_diamond', 'loyalty_activated_at',
            'loyalty_dynamic_tier_in_eligibility',
        ), 'سطح مشتری از روی امتیاز محاسبه می‌شود، نه مستقیم تعداد سفارش. «امتیاز هر سفارش» فقط در حالت '
           '«تعداد سفارش» و «مبلغ هر ۱ امتیاز» فقط در حالت «مبلغ خرید» اثر دارد. تعداد و ترتیب سطوح '
           '(مشتری جدید تا الماسی) ثابت است؛ فقط آستانه‌ی امتیاز هر سطح قابل تنظیم است.'),
        _site_settings_section('customers', 'تبدیل امتیاز به کیف‌پول', (
            'loyalty_redeem_toman_per_point', 'loyalty_redeem_min_points',
            'loyalty_redeem_max_points_per_transaction', 'loyalty_redeem_max_points_per_day',
        ), 'نرخ تبدیل و سقف‌های زیر مبنای موتور تبدیل امتیاز باشگاه به شارژ کیف‌پول است.'),
        _site_settings_section('customers', 'تنظیمات و قوانین مرجوعی کالا', (
            'return_period_days', 'return_period_unit', 'return_policy_html',
            'return_attachment_max_image_mb', 'return_attachment_max_video_mb',
        ), 'مهلت مرجوعی: «روز کاری» جمعه‌ها را نمی‌شمارد، «روز تقویمی» دقیقاً N×۲۴ ساعت از لحظه‌ی '
           'تحویل است. متن راهنما عیناً در صفحه‌ی «روش مرجوعی کالا» به مشتری نمایش داده می‌شود. '
           'سقف تعداد مدارک هر قلم (۵ فایل) ثابت است و از پنل قابل تغییر نیست.'),
    )

    def render_change_form(self, request, context, *args, **kwargs):
        # فهرست گروه‌ها برای اسکریپت تب‌ها (templates/admin/products/sitesettings/change_form.html)
        context['sitesettings_groups'] = [{'key': key, 'label': label} for key, label in SITE_SETTINGS_GROUPS]
        return super().render_change_form(request, context, *args, **kwargs)

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name == 'mega_menu_bg_color':
            kwargs['widget'] = ColorPickerWidget
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    class Media:
        css = {'all': ('products/admin/site_settings_tabs.css',)}
        js = ('products/admin/guest_pricing_toggle.js', 'products/admin/site_settings_tabs.js')

    def has_add_permission(self, request):
        return not SiteSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = SiteSettings.load()
        return redirect(reverse('admin:products_sitesettings_change', args=[obj.pk]))


@admin.register(ContactMessage)
class ContactMessageAdmin(admin.ModelAdmin):
    """
    پیام‌های فرم «تماس با ما». مدیر فقط وضعیت و پاسخ/یادداشت را ویرایش می‌کند؛ محتوای پیام و مشخصات
    فرستنده فقط‌خواندنی است (سندِ ثبت‌شده‌ی مشتری) و پیام جدید فقط از فرم سایت ساخته می‌شود.
    """
    list_display = ('created_at', 'name', 'subject', 'contact_info', 'status', 'replied_at')
    list_filter = ('status', 'created_at')
    list_editable = ('status',)
    search_fields = ('name', 'phone', 'email', 'subject', 'message')
    date_hierarchy = 'created_at'
    readonly_fields = ('name', 'phone', 'email', 'subject', 'message', 'user', 'ip_address', 'created_at', 'replied_at')
    fieldsets = (
        ('پیام مشتری', {'fields': ('name', 'phone', 'email', 'subject', 'message', 'created_at', 'user', 'ip_address')}),
        ('بررسی مدیر', {'fields': ('status', 'admin_reply', 'replied_at')}),
    )
    actions = ('mark_in_progress', 'mark_answered', 'mark_closed')

    def has_add_permission(self, request):
        return False

    @admin.display(description='راه ارتباطی')
    def contact_info(self, obj):
        return ' | '.join(part for part in (obj.phone, obj.email) if part)

    def save_model(self, request, obj, form, change):
        # ثبت یا تغییر متن پاسخ، زمان پاسخ را می‌گذارد و اگر وضعیت هنوز «جدید» بود به «پاسخ داده شد» می‌برد
        if change and 'admin_reply' in form.changed_data and obj.admin_reply.strip():
            obj.replied_at = timezone.now()
            if obj.status in (ContactMessage.STATUS_NEW, ContactMessage.STATUS_IN_PROGRESS):
                obj.status = ContactMessage.STATUS_ANSWERED
        super().save_model(request, obj, form, change)

    def _set_status(self, request, queryset, status, label):
        count = queryset.update(status=status)
        self.message_user(request, f'وضعیت {count} پیام به «{label}» تغییر کرد.', messages.SUCCESS)

    @admin.action(description='تغییر وضعیت به «در حال بررسی»')
    def mark_in_progress(self, request, queryset):
        self._set_status(request, queryset, ContactMessage.STATUS_IN_PROGRESS, 'در حال بررسی')

    @admin.action(description='تغییر وضعیت به «پاسخ داده شد»')
    def mark_answered(self, request, queryset):
        self._set_status(request, queryset, ContactMessage.STATUS_ANSWERED, 'پاسخ داده شد')

    @admin.action(description='تغییر وضعیت به «بسته شد»')
    def mark_closed(self, request, queryset):
        self._set_status(request, queryset, ContactMessage.STATUS_CLOSED, 'بسته شد')


@admin.register(StockAlert)
class StockAlertAdmin(admin.ModelAdmin):
    list_display = ('product', 'user', 'channel', 'status', 'created_at', 'notified_at')
    list_filter = ('status', 'channel', 'created_at')
    search_fields = ('product__name', 'user__phone_number', 'user__email')
    raw_id_fields = ('product', 'user')
    readonly_fields = ('created_at', 'notified_at')


@admin.register(HomeBanner)
class HomeBannerAdmin(admin.ModelAdmin):
    """
    دقیقاً ۴ جایگاه ثابت (نگاه کنید HomeBanner.SLOT_CHOICES)؛ افزودن/حذف ردیف بسته
    است تا جایگاه تکراری یا جایگاه گم‌شده پیش نیاید - فقط تصویر/لینک/فعال‌بودن هر
    جایگاه از قبل‌ساخته‌شده قابل ویرایش است.
    """
    list_display = ('banner_thumb', 'slot', 'link_summary', 'is_active')
    list_display_links = ('banner_thumb', 'slot')
    list_editable = ('is_active',)
    autocomplete_fields = ['link_product']
    fieldsets = (
        ('جایگاه و تصویر', {'fields': ('slot', 'image', 'alt_text')}),
        ('لینک مقصد', {'fields': ('link_type', 'link_url', 'link_product', 'link_category')}),
        ('نمایش', {'fields': ('is_active',)}),
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description='تصویر')
    def banner_thumb(self, obj):
        if obj.image:
            return mark_safe(f'<img src="{obj.image.url}" style="height:40px;border-radius:6px;object-fit:cover;">')
        return '—'

    @admin.display(description='لینک')
    def link_summary(self, obj):
        return dict(HomeBanner.LINK_TYPE_CHOICES).get(obj.link_type, obj.link_type)


@admin.register(HeroSlide)
class HeroSlideAdmin(admin.ModelAdmin):
    """ لیست آزاد (نه جایگاه ثابت مثل HomeBanner) - افزودن/حذف/ترتیب‌دهی آزاد است """
    list_display = ('slide_thumb', 'order', 'link_summary', 'is_active')
    list_display_links = ('slide_thumb',)
    list_editable = ('order', 'is_active')
    autocomplete_fields = ['link_product']
    fieldsets = (
        ('تصویر', {'fields': ('image', 'alt_text', 'order')}),
        ('لینک مقصد', {'fields': ('link_type', 'link_url', 'link_product', 'link_category')}),
        ('نمایش', {'fields': ('is_active',)}),
    )

    @admin.display(description='تصویر')
    def slide_thumb(self, obj):
        if obj.image:
            return mark_safe(f'<img src="{obj.image.url}" style="height:40px;border-radius:6px;object-fit:cover;">')
        return '—'

    @admin.display(description='لینک')
    def link_summary(self, obj):
        return dict(HeroSlide.LINK_TYPE_CHOICES).get(obj.link_type, obj.link_type)


@admin.register(NewsletterSubscriber)
class NewsletterSubscriberAdmin(admin.ModelAdmin):
    list_display = ('phone_number', 'created_at')
    search_fields = ('phone_number',)
    readonly_fields = ('phone_number', 'created_at')

    def has_add_permission(self, request):
        return False
