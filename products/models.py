from django.core.cache import cache
import re
from decimal import Decimal
from urllib.parse import quote, urlparse

from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator, MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models import F, Q
from accounts.models import CustomUser
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django_ckeditor_5.fields import CKEditor5Field

from services.text import normalize_persian, to_latin_digits

from .chat_settings import validate_chat_settings

from .pricing import (
    ADJUST_PERCENT, ADJUSTMENT_TYPES, GUEST_CALCULATED_PRICE, GUEST_HIDDEN_MESSAGE_DEFAULT,
    GUEST_PERCENT_MAX, GUEST_PERCENT_MIN, GUEST_PRICE_LEVEL, GUEST_PRICING_MODES, GUEST_ROUNDING_STEPS,
    price_level_choices,
)

def category_image_upload_path(instance, filename):
    """ نام‌گذاری «شناسه - نام» طبق خواسته‌ی صریح کارفرما؛ چون در اولین ذخیره هنوز pk نیست،
    save() زیر یک ذخیره‌ی دومرحله‌ای انجام می‌دهد (نگاه کنید به Category.save) """
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'jpg'
    return f'categories/{instance.pk}-{instance.name}.{ext}'


# ==========================================
# 1. گروه‌بندی کالاها (MainGroup & SideGroup هلو)
# ==========================================
class Category(models.Model):
    """ مدل دسته‌بندی با قابلیت پشتیبانی از گروه اصلی و فرعی هلو """
    name = models.CharField(max_length=200, verbose_name='نام دسته‌بندی')
    slug = models.SlugField(max_length=200, unique=True, allow_unicode=True, verbose_name='اسلاگ')

    # ارتباط درختی (نال بودن یعنی گروه اصلی است، مقدار داشتن یعنی گروه فرعی است)
    parent = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, related_name='children', verbose_name='گروه پدر')

    # شناسه هلو (MainGroupErpCode یا SideGroupErpCode)
    erp_code = models.CharField(max_length=100, blank=True, null=True, unique=True, verbose_name='شناسه گروه در هلو')
    is_active = models.BooleanField(default=True, verbose_name='فعال')

    # تصویر شاخص برای همه‌ی دسته‌ها (اصلی و زیردسته) لازم است؛ در کاروسل زیردسته‌ها و
    # دسته‌بندی پیشنهادی نمایش داده می‌شود
    featured_image = models.ImageField(upload_to=category_image_upload_path, blank=True, null=True, verbose_name='تصویر شاخص')

    # -- فیلدهای زیر فقط برای صفحه‌ی اختصاصی دسته‌های سطح‌بالا معنا دارند --
    short_description = CKEditor5Field('توضیح کوتاه', blank=True, config_name='default')

    suggested_categories = models.ManyToManyField(
        'self', symmetrical=False, blank=True, related_name='suggested_by',
        limit_choices_to={'parent__isnull': True},  # فقط دسته‌های سطح‌بالا قابل پیشنهاد شدن‌اند
        verbose_name='دسته‌بندی‌های پیشنهادی',
    )
    related_blog_categories = models.ManyToManyField(
        'blog.BlogCategory', blank=True, related_name='related_product_categories',
        verbose_name='دسته‌بندی‌های وبلاگ مرتبط',
    )

    show_amazing_deals = models.BooleanField(default=True, verbose_name='نمایش شگفت‌انگیزها')
    show_suggested_categories = models.BooleanField(default=True, verbose_name='نمایش دسته‌بندی پیشنهادی')
    show_best_sellers = models.BooleanField(default=True, verbose_name='نمایش پرفروش‌ترین‌ها')
    show_frequent = models.BooleanField(default=True, verbose_name='نمایش پرتکرارها')
    show_banners = models.BooleanField(default=True, verbose_name='نمایش بنرها')
    show_blog_posts = models.BooleanField(default=True, verbose_name='نمایش مطالب وبلاگی')

    # بنر اختصاصی مگامنوی هدر (فقط دسته‌های سطح‌بالا) - مستقل از CategoryBanner که بنرهای صفحه‌ی خودِ دسته
    # است؛ چون نسبت ابعاد بنر کنار منو (عمودی/باریک) با بنر افقی صفحه‌ی دسته فرق دارد. خالی = بدون بنر.
    # خواندنش templates/base.html (ساختار .mega-*) و نمایشش با SiteSettings.mega_menu_show_banner.
    mega_menu_banner = models.ImageField(
        upload_to='categories/mega_banners/', blank=True, null=True, verbose_name='بنر مگامنو',
        help_text='در ستون کناری مگامنوی هدر، هنگام هاور روی همین دسته نمایش داده می‌شود. خالی = بدون بنر.',
        validators=[FileExtensionValidator(['jpg', 'jpeg', 'png', 'webp'])],
    )
    mega_menu_banner_url = models.CharField(
        max_length=500, blank=True, verbose_name='لینک بنر مگامنو',
        help_text='آدرس داخلی (با / شروع شود) یا کامل (http:// یا https://). خالی = بنر بدون لینک.',
    )
    mega_menu_banner_alt = models.CharField(
        max_length=200, blank=True, verbose_name='متن جایگزین بنر مگامنو',
        help_text='برای دسترس‌پذیری و موتورهای جستجو. خالی = نام دسته‌بندی.',
    )

    class Meta:
        verbose_name = 'دسته‌بندی'
        verbose_name_plural = 'دسته‌بندی‌ها'

    def __str__(self):
        return f"{self.parent.name} -> {self.name}" if self.parent else self.name

    @staticmethod
    def _is_safe_banner_url(url):
        # فقط مسیر داخلی (نه //host) یا http(s)؛ جلوی javascript:/data: در href بنر گرفته می‌شود
        return (url.startswith('/') and not url.startswith('//')) or url.lower().startswith(('http://', 'https://'))

    @property
    def mega_menu_banner_href(self):
        """ لینک بنر مگامنو برای تمپلیت؛ حتی اگر مقدار بدون اعتبارسنجی (نوشتن مستقیم در DB) خراب شده باشد، خالی برمی‌گردد """
        url = (self.mega_menu_banner_url or '').strip()
        return url if url and self._is_safe_banner_url(url) else ''

    def clean(self):
        super().clean()
        url = (self.mega_menu_banner_url or '').strip()
        if url and not self._is_safe_banner_url(url):
            raise ValidationError({'mega_menu_banner_url': 'لینک باید با / (آدرس داخلی) یا http:// یا https:// شروع شود.'})

    def save(self, *args, **kwargs):
        # تصویر شاخص باید نامش شامل شناسه‌ی دسته باشد؛ در اولین ذخیره هنوز pk نداریم، پس
        # ابتدا بدون تصویر ذخیره می‌کنیم تا pk ساخته شود، بعد در یک ذخیره‌ی دوم تصویر را
        # می‌نشانیم تا upload_to این‌بار با self.pk واقعی صدا زده شود
        if self.pk is None:
            pending_image = self.featured_image
            self.featured_image = None
            super().save(*args, **kwargs)
            if pending_image:
                self.featured_image = pending_image
                super().save(update_fields=['featured_image'])
        else:
            super().save(*args, **kwargs)

    def get_ancestors(self, include_self=True):
        """
        زنجیره‌ی والدین از بالاترین سطح تا خود دسته، برای ساخت breadcrumb. با include_self=False
        فقط والدین (بدون خود دسته) برگردانده می‌شود.
        """
        chain = []
        node = self if include_self else self.parent
        while node:
            chain.append(node)
            node = node.parent
        chain.reverse()
        return chain

    def get_descendant_ids(self, include_self=True):
        """
        شناسه‌ی خود + همه‌ی فرزندان در هر عمقی (BFS روی parent/children)، بدون نیاز
        به migration یا کتابخانه‌ی درخت (mptt و...). برای فیلتر کردن محصولات یک دسته
        به همراه همه‌ی زیردسته‌هایش (نه فقط یک سطح) استفاده می‌شود.
        """
        ids = [self.id] if include_self else []
        frontier = [self.id]
        while frontier:
            child_ids = list(Category.objects.filter(parent_id__in=frontier).values_list('id', flat=True))
            if not child_ids:
                break
            ids.extend(child_ids)
            frontier = child_ids
        return ids


class CategoryBanner(models.Model):
    """ بنر تبلیغاتی یک دسته (حداکثر ۵ عدد، محدودیت در سطح ادمین/inline، نه دیتابیس) """
    category = models.ForeignKey(Category, related_name='banners', on_delete=models.CASCADE, verbose_name='دسته‌بندی')
    image = models.ImageField(upload_to='categories/banners/', verbose_name='تصویر بنر')
    link_product = models.ForeignKey(
        'Product', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='+', verbose_name='محصول مقصد (اختیاری)',
    )
    link_url = models.CharField(max_length=500, blank=True, verbose_name='لینک مقصد (در صورت نبود محصول)')
    order = models.PositiveIntegerField(default=0, verbose_name='ترتیب نمایش')

    class Meta:
        verbose_name = 'بنر دسته‌بندی'
        verbose_name_plural = 'بنرهای دسته‌بندی'
        ordering = ('order', 'id')

    def __str__(self):
        return f"بنر {self.category.name} #{self.pk}"

    @property
    def target_url(self):
        if self.link_product_id:
            return reverse('products:product_detail', args=[self.link_product.slug])
        return self.link_url or '#'


class VisibleStoryManager(models.Manager):
    """ فقط استوری‌های فعال و داخل بازه‌ی زمانی نمایش (خالی = بدون محدودیت)؛ هم‌الگوی VisibleProductManager """

    def get_queryset(self):
        now = timezone.now()
        return super().get_queryset().filter(is_active=True).filter(
            models.Q(starts_at__isnull=True) | models.Q(starts_at__lte=now)
        ).filter(
            models.Q(ends_at__isnull=True) | models.Q(ends_at__gte=now)
        )


class Story(models.Model):
    """ استوری اینستاگرامی صفحه اصلی (ردیف دایره‌ها بالای اسلایدر) - عکس یا فیلم """
    IMAGE = 'image'
    VIDEO = 'video'
    TYPE_CHOICES = ((IMAGE, 'عکس'), (VIDEO, 'فیلم'))

    title = models.CharField(max_length=100, verbose_name='عنوان (زیر دایره)')
    story_type = models.CharField(max_length=10, choices=TYPE_CHOICES, default=IMAGE, verbose_name='نوع استوری')

    cover_image = models.ImageField(
        upload_to='stories/covers/', verbose_name='تصویر دایره (کاور)',
        help_text='برای هر دو نوع الزامی است؛ حتی برای استوری فیلم، همین تصویر در ردیف دایره‌ها نشان داده می‌شود.',
    )
    image = models.ImageField(
        upload_to='stories/images/', blank=True, null=True, verbose_name='تصویر استوری',
        help_text='فقط برای نوع «عکس» پر شود.',
        validators=[FileExtensionValidator(['jpg', 'jpeg', 'png', 'webp'])],
    )
    video = models.FileField(
        upload_to='stories/videos/', blank=True, null=True, verbose_name='فیلم استوری',
        help_text='فقط برای نوع «فیلم» پر شود.',
        validators=[FileExtensionValidator(['mp4', 'webm', 'mov'])],
    )
    duration_ms = models.PositiveIntegerField(
        default=5000, verbose_name='مدت نمایش (میلی‌ثانیه)',
        help_text='فقط برای نوع «عکس» - مدت زمان قبل از رفتن به استوری بعدی. برای فیلم نادیده گرفته می‌شود (مدت واقعی فیلم استفاده می‌شود).',
    )

    link_product = models.ForeignKey(
        'Product', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='+', verbose_name='محصول مقصد (اختیاری)',
    )
    link_url = models.CharField(max_length=500, blank=True, verbose_name='لینک مقصد (در صورت نبود محصول)')

    starts_at = models.DateTimeField(null=True, blank=True, verbose_name='شروع نمایش', help_text='خالی = از همین الان')
    ends_at = models.DateTimeField(null=True, blank=True, verbose_name='پایان نمایش', help_text='خالی = بدون انقضا')

    order = models.PositiveIntegerField(default=0, verbose_name='ترتیب نمایش')
    is_active = models.BooleanField(default=True, verbose_name='فعال')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = models.Manager()
    visible = VisibleStoryManager()

    class Meta:
        verbose_name = 'استوری'
        verbose_name_plural = 'استوری‌ها'
        ordering = ('order', '-created_at')

    def __str__(self):
        return self.title

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.story_type == self.IMAGE and not self.image:
            raise ValidationError({'image': 'برای استوری از نوع «عکس»، فیلد تصویر استوری الزامی است.'})
        if self.story_type == self.VIDEO and not self.video:
            raise ValidationError({'video': 'برای استوری از نوع «فیلم»، فیلد فیلم استوری الزامی است.'})

    @property
    def media_url(self):
        return self.video.url if self.story_type == self.VIDEO else self.image.url

    @property
    def target_url(self):
        if self.link_product_id:
            return reverse('products:product_detail', args=[self.link_product.slug])
        return self.link_url or ''


# ==========================================
# برند (کاملاً مستقل از هلو، مدیریت دستی در ادمین سایت)
# ==========================================
class Brand(models.Model):
    """ برند محصول (مثلاً شیائومی، اپل و ...) - داده‌ای صرفاً نمایشی برای سایت """
    name = models.CharField(max_length=150, unique=True, verbose_name='نام برند')
    slug = models.SlugField(max_length=150, unique=True, allow_unicode=True, verbose_name='اسلاگ')
    logo = models.ImageField(upload_to='brands/', blank=True, null=True, verbose_name='لوگو')
    is_active = models.BooleanField(default=True, verbose_name='فعال')

    class Meta:
        verbose_name = 'برند'
        verbose_name_plural = 'برندها'
        ordering = ('name',)

    def __str__(self):
        return self.name


# ==========================================
# گارانتی (کاملاً مستقل از هلو، مدیریت دستی در ادمین سایت)
# ==========================================
class Warranty(models.Model):
    """ گارانتی محصول (مثلاً ۱۸ ماه گارانتی شرکتی و ...) - داده‌ای صرفاً نمایشی برای سایت """
    name = models.CharField(max_length=150, unique=True, verbose_name='عنوان گارانتی')
    is_active = models.BooleanField(default=True, verbose_name='فعال')

    class Meta:
        verbose_name = 'گارانتی'
        verbose_name_plural = 'گارانتی‌ها'
        ordering = ('name',)

    def __str__(self):
        return self.name


class VisibleProductManager(models.Manager):
    """ فقط محصولاتی که هم فعال هستند و هم قیمت فروشی برایشان ثبت شده (تازه‌سینک‌شده از هلو و بی‌قیمت نیستند) """

    def get_queryset(self):
        return super().get_queryset().filter(is_active=True, price__gt=0)


# ==========================================
# 2. هسته اصلی محصول (متصل به هلو)
# ==========================================
class Product(models.Model):
    category = models.ForeignKey(Category, related_name='products', on_delete=models.SET_NULL, null=True, verbose_name='دسته‌بندی فرعی')
    brand = models.ForeignKey(Brand, related_name='products', on_delete=models.SET_NULL, null=True, blank=True, verbose_name='برند')
    name = models.CharField(max_length=255, verbose_name='نام کالا')
    # برای جستجو: نسخه‌ی یکدست‌شده‌ی name (حروف عربی/فارسی مشابه، نیم‌فاصله، اعراب و ارقام) - در save() ساخته می‌شود
    name_normalized = models.CharField(max_length=255, blank=True, db_index=True, editable=False, verbose_name='نام برای جستجو')
    slug = models.SlugField(max_length=255, unique=True, allow_unicode=True, verbose_name='اسلاگ')
    
    # -- فیلدهای یکپارچه با هلو (مالی و انبار) --
    erp_code = models.CharField(max_length=100, unique=True, db_index=True, verbose_name='ErpCode هلو')
    product_code = models.CharField(max_length=50, blank=True, null=True, verbose_name='کد کالا') # مثلا 00202010 در عکس شما
    
    # قیمت‌ها: قیمت اصلی را SellPrice میگیریم. 
    price = models.DecimalField(max_digits=12, decimal_places=0, default=0, verbose_name='قیمت فروش 1')
    # فیلد جدید: واحد کالا (پیش‌فرض عدد)
    unit = models.CharField(max_length=50, default='عدد', verbose_name='واحد کالا')
    # --- قیمت‌های سطوح مختلف هلو ---
    price2 = models.DecimalField(max_digits=12, decimal_places=0, default=0, verbose_name='قیمت فروش 2')
    price3 = models.DecimalField(max_digits=12, decimal_places=0, default=0, verbose_name='قیمت فروش 3')
    price4 = models.DecimalField(max_digits=12, decimal_places=0, default=0, verbose_name='قیمت فروش 4')
    price5 = models.DecimalField(max_digits=12, decimal_places=0, default=0, verbose_name='قیمت فروش 5')
    price6 = models.DecimalField(max_digits=12, decimal_places=0, default=0, verbose_name='قیمت فروش 6')
    price7 = models.DecimalField(max_digits=12, decimal_places=0, default=0, verbose_name='قیمت فروش 7')
    price8 = models.DecimalField(max_digits=12, decimal_places=0, default=0, verbose_name='قیمت فروش 8')
    price9 = models.DecimalField(max_digits=12, decimal_places=0, default=0, verbose_name='قیمت فروش 9')
    price10 = models.DecimalField(max_digits=12, decimal_places=0, default=0, verbose_name='قیمت فروش 10')
    
    stock = models.FloatField(default=0, verbose_name='موجودی')
    # دو ستون با دو مالک: stock فقط توسط سینک هلو (holoo/product_state.py) نوشته می‌شود و reserved_quantity فقط توسط
    # رزرو اتمیک سفارش‌های سایت (products/stock.py). موجودی قابل‌فروش = stock − reserved_quantity − بافر اطمینان و ذخیره نمی‌شود.
    reserved_quantity = models.PositiveIntegerField(default=0, editable=False, verbose_name='موجودی رزروشده (سفارش‌های سایت)')
    # لحظه‌ی *شروع* واکشی‌ای که این مقدارها را آورد (نه لحظه‌ی نوشتن)؛ نوشتنِ قدیمی‌تر روی جدیدتر نمی‌نشیند و آزادسازی
    # رزرو فاکتورشده به آن تکیه دارد.
    stock_synced_at = models.DateTimeField(null=True, blank=True, editable=False, verbose_name='آخرین همگام‌سازی موجودی')
    price_synced_at = models.DateTimeField(null=True, blank=True, editable=False, verbose_name='آخرین همگام‌سازی قیمت')
    
    # -- فیلدهای اختصاصی وب‌سایت (نمایشی) --
    description = models.TextField(blank=True, null=True, verbose_name='توضیحات معرفی')
    additional_description = CKEditor5Field('توضیحات تکمیلی', blank=True, config_name='default')
    main_image = models.ImageField(upload_to='products/main/', blank=True, null=True, verbose_name='تصویر اصلی سایت')
    warranty = models.ForeignKey(Warranty, related_name='products', on_delete=models.SET_NULL, null=True, blank=True, verbose_name='گارانتی')

    is_active = models.BooleanField(default=True, verbose_name='نمایش در سایت')
    free_shipping = models.BooleanField(default=False, verbose_name='ارسال رایگان')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = models.Manager()
    # محصولات قابل‌نمایش در سایت (فعال + دارای قیمت فروش)؛ برای صفحات فروشگاه/سبد/علاقه‌مندی/مقایسه استفاده شود
    visible = VisibleProductManager()

    class Meta:
        verbose_name = 'محصول'
        verbose_name_plural = 'محصولات'
        indexes = [models.Index(fields=['erp_code'])]
        
    def get_user_price(self, user):
        """
        قیمت واحد کاربر بر اساس سطح قیمتش، بدون تخفیف (برای مبلغ خط‌خورده‌ی کنار قیمت تخفیف‌خورده).
        اگر قیمت آن سطح صفر بود (در هلو پر نشده بود)، قیمت سطح ۱ (چکی) برمی‌گردد.

        پیاده‌سازی به pricing._price_for_level/_price_level واگذار شده تا یک منبع مشترک هم برای کاربر
        واردشده و هم برای مهمان داشته باشیم؛ برای مهمان سطح از SiteSettings.guest_price_level می‌آید
        (پیش‌فرض ۱)، نه همیشه سطح ۱ ثابت مثل قبل.
        """
        from .pricing import _price_for_level, _price_level
        return _price_for_level(self, _price_level(user))

    def get_secondary_price(self, user):
        """
        برای مشتریان چکی (سطح ۱) و نقدی (سطح ۲)، قیمتِ نوع دیگر (غیر از سطح خودشان) را
        برمی‌گرداند تا در کنار قیمت اصلی (بزرگ‌تر، از get_user_price) به‌صورت کوچک‌تر نمایش
        داده شود. برای کاربر ویژه یا مهمان (که دو نوع قیمت برایشان معنا ندارد) یا وقتی قیمت
        نوع دیگر در هلو ثبت نشده (صفر)، None برمی‌گرداند.
        خروجی: {'label': 'چکی' یا 'نقدی', 'price': Decimal} یا None
        """
        if not (user and user.is_authenticated):
            return None

        level = getattr(user, 'price_level', 1)
        if level == 1:
            other_price, other_label = self.price2, 'نقدی'
        elif level == 2:
            other_price, other_label = self.price, 'چکی'
        else:
            return None

        if not other_price or other_price <= 0:
            return None
        return {'label': other_label, 'price': other_price}

    def get_discounted_price(self, user, method=None):
        """
        قیمت نهایی یک واحد کالا برای این کاربر (سطح قیمت/روش پرداخت + تخفیف‌های خودکار اپ promotions).
        پیاده‌سازی عمداً به products/pricing.py واگذار شده تا کارت محصول، سبد خرید و فاکتور
        همگی از یک فرمول واحد استفاده کنند (قبلاً هرکدام محاسبه‌ی جدا داشتند و تخفیف فقط
        روی کارت اعمال می‌شد، نه در سبد و فاکتور).
        """
        from .pricing import final_price
        return final_price(self, user, method)

    PLACEHOLDER_IMAGE_STATIC_PATH = 'theme/assets/images/Preload.webp'
    HOVER_PLACEHOLDER_IMAGE_STATIC_PATH = 'theme/assets/images/Preload-2.webp'

    @property
    def main_image_url(self):
        """
        آدرس قطعی تصویر اصلی برای نمایش در فرانت. اگر main_image خالی/ناموجود بود (کالای هنوز
        بدون عکس از اسکنر products/services.py::sync_product_images)، ابتدا «نو ایمیج ۱» تنظیمات
        سایت (در صورت آپلود) و در نبود آن آدرس استاتیک پیش‌فرض تم (Preload.webp) برگردانده
        می‌شود - بدون نوشتن هیچ مسیر فیکی در دیتابیس - تا هیچ صفحه‌ای با آیکن شکسته یا جای خالی
        رندر نشود.
        """
        if self.main_image:
            return self.main_image.url
        no_image_1 = SiteSettings.cached().no_image_1
        if no_image_1:
            return no_image_1.url
        return static(self.PLACEHOLDER_IMAGE_STATIC_PATH)

    @property
    def hover_image_url(self):
        """
        آدرس تصویر دوم/گالری برای جلوه‌ی Hover کارت محصول (تم آرینو با ماوس‌رفتن روی کارت این
        تصویر را جایگزین main_image می‌کند). اگر محصول تصویر گالری نداشت ولی main_image واقعی
        داشت، همان main_image برگردانده می‌شود تا جلوه‌ی هاور روی تک‌عکسی‌ها زوم روی همان عکس
        باشد، نه نمایش یک تصویر پیش‌فرض نامرتبط. فقط وقتی محصول اصلاً هیچ عکسی (نه اصلی، نه
        گالری) نداشته باشد، «نو ایمیج ۲» تنظیمات سایت و در نبود آن Preload-2.webp استفاده می‌شود.
        """
        second_image = self.gallery_images.first()
        if second_image:
            return second_image.image.url
        if self.main_image:
            return self.main_image_url
        no_image_2 = SiteSettings.cached().no_image_2
        if no_image_2:
            return no_image_2.url
        return static(self.HOVER_PLACEHOLDER_IMAGE_STATIC_PATH)

    def save(self, *args, **kwargs):
        self.name_normalized = normalize_persian(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

    @property
    def available_quantity(self):
        """ موجودی قابل‌فروش برای مشتری: floor(موجودی هلو) − رزروشده − بافر اطمینان (products/stock.py) """
        from .stock import available_of
        return available_of(self)

    @property
    def is_available(self):
        return self.available_quantity > 0


class ProductImage(models.Model):
    """ تصاویر گالری محصول؛ تصویر اصلی (main_image) همیشه اسلاید اول است و این‌ها بعد از آن می‌آیند """
    product = models.ForeignKey(Product, related_name='gallery_images', on_delete=models.CASCADE, verbose_name='محصول')
    image = models.ImageField(upload_to='products/gallery/', verbose_name='تصویر')
    order = models.PositiveIntegerField(default=0, verbose_name='ترتیب نمایش')

    class Meta:
        verbose_name = 'تصویر گالری محصول'
        verbose_name_plural = 'گالری تصاویر محصول'
        ordering = ('order', 'id')

    def __str__(self):
        return f"تصویر گالری {self.product.name} #{self.pk}"


class ProductColor(models.Model):
    """ رنگ‌بندی نمایشی محصول (موجودی/قیمت مشترک با کل محصول است، فقط برای نمایش گزینه‌ی رنگ) """
    product = models.ForeignKey(Product, related_name='colors', on_delete=models.CASCADE, verbose_name='محصول')
    name = models.CharField(max_length=50, verbose_name='نام رنگ')
    hex_code = models.CharField(max_length=7, default='#000000', verbose_name='کد رنگ (Hex)')
    is_default = models.BooleanField(default=False, verbose_name='رنگ پیش‌فرض')
    order = models.PositiveIntegerField(default=0, verbose_name='ترتیب نمایش')

    class Meta:
        verbose_name = 'رنگ محصول'
        verbose_name_plural = 'رنگ‌بندی محصول'
        ordering = ('order', 'id')

    def __str__(self):
        return f"{self.product.name} - {self.name}"


# ==========================================
# 3. راه حل مشخصات فنی (الگوی EAV)
# ==========================================
class Feature(models.Model):
    """ تعریف عناوین مشخصات (مثل: جنسیت، مدل، نوت عطر، مکان، سازنده) """
    name = models.CharField(max_length=100, unique=True, verbose_name='نام ویژگی')

    class Meta:
        verbose_name = 'ویژگی'
        verbose_name_plural = 'ویژگی‌ها'

    def __str__(self):
        return self.name

class ProductFeatureValue(models.Model):
    """ مقداردهی مشخصات برای هر محصول (مثل: برای محصول X، جنسیت = زنانه) """
    product = models.ForeignKey(Product, related_name='features', on_delete=models.CASCADE, verbose_name='محصول')
    feature = models.ForeignKey(Feature, related_name='values', on_delete=models.CASCADE, verbose_name='ویژگی')
    value = models.CharField(max_length=255, verbose_name='مقدار')

    class Meta:
        verbose_name = 'مقدار ویژگی'
        verbose_name_plural = 'مشخصات فنی محصولات'
        unique_together = ('product', 'feature') # هر محصول یک ویژگی را فقط یک بار می‌تواند داشته باشد

    def __str__(self):
        return f"{self.product.name} - {self.feature.name}: {self.value}"


# ==========================================
# 4. تنظیمات سراسری سایت (فوتر/تماس/شبکه‌های اجتماعی)
# ==========================================
def format_grouped_number(number, persian):
    """ عدد صحیح با جداکننده‌ی هزارگان؛ persian=True: ارقام فارسی و «٬» (مثلاً ۵٬۰۰۰)، وگرنه لاتین و «,» (5,000) """
    text = f'{number:,}'
    if not persian:
        return text
    persian_digits = str.maketrans('0123456789,', '۰۱۲۳۴۵۶۷۸۹٬')
    return text.translate(persian_digits)


def is_valid_iranian_national_code(code):
    """ اعتبارسنجی کد ملی ۱۰ رقمی ایران با رقم کنترل (الگوریتم استاندارد) """
    if not re.fullmatch(r'\d{10}', code) or len(set(code)) == 1:
        return False
    total = sum(int(code[i]) * (10 - i) for i in range(9))
    remainder = total % 11
    return int(code[9]) == (remainder if remainder < 2 else 11 - remainder)


class SiteSettings(models.Model):
    """ تک‌ردیفی (singleton)؛ تنظیمات فوتر که در همه‌ی صفحات از طریق context processor در دسترس است """
    # فقط برای شکستن کش مرورگر روی تصاویر برندسازی (فاوآیکون/لوگو) با ?v=timestamp؛ به هیچ منطق
    # دیگری وابسته نیست - نگاه کنید base.html
    updated_at = models.DateTimeField(auto_now=True, verbose_name='آخرین تغییر')

    # --- هویت و مشخصات فروشگاه: مرجع واحد داده برای هدر، فوتر، صفحه‌های «درباره ما»/«تماس با ما»، اسناد و
    # اعلان‌ها (به‌جای متن‌های هاردکد در قالب‌ها). store_phone_1 / store_email_1 / store_working_hours همان
    # فیلدهای قدیمی phone / email / working_hours_text‌اند که با RenameField (بدون از دست رفتن داده) تغییر نام
    # گرفتند - نگاه کنید مایگریشن 0039. ---
    store_name = models.CharField(
        max_length=200, default='بازرگانی موسوی', verbose_name='نام تجاری فروشگاه',
        help_text='در هدر، فوتر، عنوان صفحه‌ها (title)، متن جایگزین لوگو و صفحه‌های «درباره ما» و «تماس با ما» نمایش داده می‌شود.',
    )
    store_legal_name = models.CharField(
        max_length=200, blank=True, verbose_name='نام ثبتی / شخصیت حقوقی',
        help_text='نام ثبت‌شده در اسناد رسمی (مثلاً «شرکت ... (سهامی خاص)»)؛ برای اسناد و فاکتور.',
    )
    store_national_id = models.CharField(
        max_length=11, blank=True, verbose_name='شناسه ملی / کد ملی',
        help_text='شناسه ملی شخص حقوقی (۱۱ رقم) یا کد ملی شخص حقیقی (۱۰ رقم).',
    )
    store_registration_number = models.CharField(
        max_length=20, blank=True, verbose_name='شماره ثبت', help_text='فقط رقم.',
    )
    store_economic_code = models.CharField(
        max_length=14, blank=True, verbose_name='کد اقتصادی', help_text='۱۲ یا ۱۴ رقم.',
    )
    store_postal_code = models.CharField(
        max_length=10, blank=True, verbose_name='کد پستی', help_text='۱۰ رقم، بدون خط تیره.',
    )
    store_address = models.TextField(blank=True, verbose_name='آدرس کامل فروشگاه')
    # --- فاکتور رسمی (سفارش و برگشت از فروش): مهر/امضا و سوییچ‌های سربرگ. هر مورد سربرگ فقط وقتی چاپ می‌شود که هم سوییچش
    # روشن باشد و هم مقدارش (store_legal_name و ...) خالی نباشد؛ آدرس/کد پستی/تلفن سوییچ ندارند و اگر پر باشند چاپ می‌شوند.
    store_stamp_image = models.ImageField(
        upload_to='branding/', blank=True, verbose_name='تصویر مهر/امضای فروشگاه',
        help_text='تصویر مهر یا امضای رسمی (ترجیحاً PNG شفاف) که پایین فاکتور چاپ می‌شود. اختیاری.',
    )
    invoice_show_legal_name = models.BooleanField(
        default=True, verbose_name='نمایش نام حقوقی در فاکتور',
        help_text='اگر خاموش یا نام ثبتی خالی باشد، نام تجاری فروشگاه در سربرگ فاکتور چاپ می‌شود.',
    )
    invoice_show_national_id = models.BooleanField(default=True, verbose_name='نمایش شناسه ملی در فاکتور')
    invoice_show_registration_number = models.BooleanField(default=True, verbose_name='نمایش شماره ثبت در فاکتور')
    invoice_show_economic_code = models.BooleanField(default=True, verbose_name='نمایش کد اقتصادی در فاکتور')
    invoice_show_stamp = models.BooleanField(
        default=True, verbose_name='نمایش مهر فروشگاه در فاکتور',
        help_text='فقط وقتی مهر چاپ می‌شود که تصویر مهر هم بارگذاری شده باشد.',
    )
    store_phone_1 = models.CharField(max_length=32, blank=True, verbose_name='تلفن ثابت ۱')
    store_phone_2 = models.CharField(max_length=32, blank=True, verbose_name='تلفن ثابت ۲')
    store_mobile = models.CharField(
        max_length=11, blank=True, verbose_name='شماره همراه فروشگاه', help_text='با فرمت 09123456789.',
    )
    store_admin_sms_recipient = models.CharField(
        max_length=11, blank=True, verbose_name='شماره موبایل مدیر ۱ برای دریافت پیامک‌های سیستمی',
        help_text='پیامک «پیام جدید از تماس با ما» به همین شماره (و شماره‌ی دوم زیر، اگر پر باشد) می‌رود '
                  '(فرمت 09123456789). هر دو خالی = از شماره‌ی پیش‌فرض اعلان مدیر در تنظیمات سرور استفاده می‌شود.',
    )
    store_admin_sms_recipient_2 = models.CharField(
        max_length=11, blank=True, verbose_name='شماره موبایل مدیر ۲ (اختیاری)',
        help_text='برای اطمینان بیشتر از رسیدن پیامک؛ اگر پر باشد پیامک به هر دو شماره می‌رود. همان شماره‌ی بالا '
                  'را تکرار نکنید (پیامک دوبار برای یک شماره نمی‌رود).',
    )
    store_email_1 = models.EmailField(blank=True, verbose_name='ایمیل رسمی')
    store_email_2 = models.EmailField(blank=True, verbose_name='ایمیل پشتیبانی')
    store_working_hours = models.TextField(
        blank=True, default='هفت روز هفته، ۲۴ ساعت شبانه‌روز پاسخگوی شما هستیم.',
        verbose_name='ساعات کاری',
        help_text='هر خط یک ردیف. برای نمایش دوستونه (روز و ساعت) از «:» استفاده کنید، مثلاً '
                  '«شنبه تا چهارشنبه: ۸ صبح تا ۱۷» و «جمعه: تعطیل». خط بدون «:» به‌صورت متن تمام‌عرض نمایش داده می‌شود.',
    )

    # --- نقشه‌ی صفحه‌ی «تماس با ما» ---
    MAP_GOOGLE = 'google'
    MAP_NESHAN = 'neshan'
    MAP_CUSTOM = 'custom'
    MAP_TYPE_CHOICES = (
        (MAP_GOOGLE, 'نقشه‌ی گوگل (از روی مختصات)'),
        (MAP_NESHAN, 'نشان / بلد (نقشه‌ی متن‌باز از روی مختصات + دکمه‌ی لینک نشان/بلد)'),
        (MAP_CUSTOM, 'کد iframe دلخواه'),
    )
    MAP_IFRAME_ALLOWED_HOSTS = (
        'www.google.com', 'google.com', 'maps.google.com', 'www.openstreetmap.org', 'neshan.org', 'www.neshan.org',
        'balad.ir', 'www.balad.ir',
    )
    map_type = models.CharField(
        max_length=10, choices=MAP_TYPE_CHOICES, default=MAP_GOOGLE, verbose_name='نوع نقشه',
        help_text='نقشه‌ی رسمی نشان/بلد برای جاسازی به کلید API نیاز دارد؛ بنابراین گزینه‌ی «نشان / بلد» نقشه را از '
                  'OpenStreetMap (بدون کلید) با همان مختصات نمایش می‌دهد و دکمه‌ی «مشاهده در نشان/بلد» را از '
                  'فیلد «لینک نشان/بلد» می‌سازد. با خالی بودن مختصات (یا کد iframe) نقشه نمایش داده نمی‌شود.',
    )
    map_latitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True, verbose_name='عرض جغرافیایی (Latitude)',
        help_text='مثلاً 35.689197 (بین ‎-90 تا 90).',
    )
    map_longitude = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True, verbose_name='طول جغرافیایی (Longitude)',
        help_text='مثلاً 51.388974 (بین ‎-180 تا 180).',
    )
    map_neshan_url = models.URLField(
        blank=True, verbose_name='لینک نشان / بلد',
        help_text='لینک اشتراک‌گذاری موقعیت از اپ نشان یا بلد؛ دکمه‌ی «مشاهده در نشان/بلد» به همین می‌رود. اختیاری.',
    )
    map_iframe_code = models.TextField(
        blank=True, verbose_name='کد iframe نقشه (برای نوع «دلخواه»)',
        help_text='کد جاسازی (Embed) نقشه، مثلاً از گوگل‌مپ. فقط آدرس (src) آن و فقط از دامنه‌های مجاز '
                  '(گوگل، OpenStreetMap، نشان، بلد) با https پذیرفته می‌شود؛ بقیه‌ی کد دور ریخته می‌شود.',
    )

    # --- محتوای صفحه‌ی «درباره ما» (همه‌چیز اختیاری؛ بخش خالی در صفحه نمایش داده نمی‌شود) ---
    ABOUT_ICONS = {
        'quality': ('کیفیت (نمودار)', 'M3.75 3v11.25A2.25 2.25 0 0 0 6 16.5h2.25M3.75 3h-1.5m1.5 0h16.5m0 0h1.5m-1.5 0v11.25A2.25 2.25 0 0 1 18 16.5h-2.25m-7.5 0h7.5m-7.5 0-1 3m8.5-3 1 3m0 0 .5 1.5m-.5-1.5h-9.5m0 0-.5 1.5m.75-9 3-3 2.148 2.148A12.061 12.061 0 0 1 16.5 7.605'),
        'price': ('قیمت (سکه)', 'M12 6v12m-3-2.818.879.659c1.171.879 3.07.879 4.242 0 1.172-.879 1.172-2.303 0-3.182C13.536 12.219 12.768 12 12 12c-.725 0-1.45-.22-2.003-.659-1.106-.879-1.106-2.303 0-3.182s2.9-.879 4.006 0l.415.33M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z'),
        'support': ('پشتیبانی (تلفن)', 'M2.25 6.75c0 8.284 6.716 15 15 15h2.25a2.25 2.25 0 0 0 2.25-2.25v-1.372c0-.516-.351-.966-.852-1.091l-4.423-1.106c-.44-.11-.902.055-1.173.417l-.97 1.293c-.282.376-.769.542-1.21.38a12.035 12.035 0 0 1-7.143-7.143c-.162-.441.004-.928.38-1.21l1.293-.97c.363-.271.527-.734.417-1.173L6.963 3.102a1.125 1.125 0 0 0-1.091-.852H4.5A2.25 2.25 0 0 0 2.25 4.5v2.25Z'),
        'shield': ('اطمینان (سپر)', 'M9 12.75 11.25 15 15 9.75m-3-7.036A11.959 11.959 0 0 1 3.598 6 11.99 11.99 0 0 0 3 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285Z'),
        'heart': ('رضایت (قلب)', 'M21 8.25c0-2.485-2.099-4.5-4.688-4.5-1.935 0-3.597 1.126-4.312 2.733-.715-1.607-2.377-2.733-4.313-2.733C5.1 3.75 3 5.765 3 8.25c0 7.22 9 12 9 12s9-4.78 9-12Z'),
        'star': ('برتری (ستاره)', 'M11.48 3.499a.562.562 0 0 1 1.04 0l2.125 5.111a.563.563 0 0 0 .475.345l5.518.442c.499.04.701.663.321.988l-4.204 3.602a.563.563 0 0 0-.182.557l1.285 5.385a.562.562 0 0 1-.84.61l-4.725-2.885a.562.562 0 0 0-.586 0L6.982 20.54a.562.562 0 0 1-.84-.61l1.285-5.386a.562.562 0 0 0-.182-.557l-4.204-3.602a.562.562 0 0 1 .321-.988l5.518-.442a.563.563 0 0 0 .475-.345L11.48 3.5Z'),
    }
    ABOUT_ICON_CHOICES = tuple((key, label) for key, (label, _path) in ABOUT_ICONS.items())
    about_story_title = models.CharField(max_length=200, blank=True, default='داستان ما', verbose_name='عنوان داستان ما')
    about_story_text = models.TextField(
        blank=True, verbose_name='متن داستان ما',
        help_text='هر خط خالی یک پاراگراف جدید می‌سازد. خالی = این بخش در صفحه نمایش داده نمی‌شود.',
    )
    about_story_image = models.ImageField(upload_to='about/', blank=True, verbose_name='تصویر داستان ما')
    about_value1_title = models.CharField(max_length=100, blank=True, verbose_name='کارت ۱ - عنوان')
    about_value1_icon = models.CharField(max_length=10, choices=ABOUT_ICON_CHOICES, default='quality', verbose_name='کارت ۱ - آیکون')
    about_value1_text = models.TextField(blank=True, verbose_name='کارت ۱ - متن')
    about_value2_title = models.CharField(max_length=100, blank=True, verbose_name='کارت ۲ - عنوان')
    about_value2_icon = models.CharField(max_length=10, choices=ABOUT_ICON_CHOICES, default='price', verbose_name='کارت ۲ - آیکون')
    about_value2_text = models.TextField(blank=True, verbose_name='کارت ۲ - متن')
    about_value3_title = models.CharField(max_length=100, blank=True, verbose_name='کارت ۳ - عنوان')
    about_value3_icon = models.CharField(max_length=10, choices=ABOUT_ICON_CHOICES, default='support', verbose_name='کارت ۳ - آیکون')
    about_value3_text = models.TextField(blank=True, verbose_name='کارت ۳ - متن')
    about_stat1_value = models.CharField(max_length=20, blank=True, verbose_name='آمار ۱ - عدد شاخص', help_text='مثلاً ۱۰+')
    about_stat1_label = models.CharField(max_length=60, blank=True, verbose_name='آمار ۱ - عنوان', help_text='مثلاً سال سابقه')
    about_stat2_value = models.CharField(max_length=20, blank=True, verbose_name='آمار ۲ - عدد شاخص')
    about_stat2_label = models.CharField(max_length=60, blank=True, verbose_name='آمار ۲ - عنوان', help_text='مثلاً مشتری راضی')
    about_stat3_value = models.CharField(max_length=20, blank=True, verbose_name='آمار ۳ - عدد شاخص')
    about_stat3_label = models.CharField(max_length=60, blank=True, verbose_name='آمار ۳ - عنوان', help_text='مثلاً تنوع کالا')
    about_stat4_value = models.CharField(max_length=20, blank=True, verbose_name='آمار ۴ - عدد شاخص')
    about_stat4_label = models.CharField(max_length=60, blank=True, verbose_name='آمار ۴ - عنوان', help_text='مثلاً رضایت مشتریان')

    footer_about_title = models.CharField(max_length=200, blank=True, default='فروشگاه اینترنتی هلو', verbose_name='عنوان درباره‌ی فروشگاه (فوتر)')
    footer_about_text = models.TextField(
        blank=True, default='خرید آنلاین محصولات با تحویل سریع و پشتیبانی مستقیم.',
        verbose_name='متن درباره‌ی فروشگاه (فوتر)',
    )
    copyright_text = models.CharField(
        max_length=300, blank=True,
        default='کلیه حقوق این سایت متعلق به فروشگاه هلو می‌باشد.',
        verbose_name='متن کپی‌رایت',
    )

    enamad_link = models.URLField(blank=True, verbose_name='لینک اینماد')
    trust_seal_link = models.URLField(blank=True, verbose_name='لینک نماد اعتماد الکترونیک (trust-seals)')
    samandehi_link = models.URLField(blank=True, verbose_name='لینک نماد ساماندهی')

    rubika_url = models.URLField(blank=True, verbose_name='لینک روبیکا')
    aparat_url = models.URLField(blank=True, verbose_name='لینک آپارات')
    bale_url = models.URLField(blank=True, verbose_name='لینک بله')
    eitaa_url = models.URLField(blank=True, verbose_name='لینک ایتا')
    igap_url = models.URLField(blank=True, verbose_name='لینک آی‌گپ')
    soroush_url = models.URLField(blank=True, verbose_name='لینک سروش')
    instagram_url = models.URLField(blank=True, verbose_name='لینک اینستاگرام')
    telegram_url = models.URLField(blank=True, verbose_name='لینک تلگرام')

    # هزینه‌ی ارسال دیگر عدد ثابت نیست: کرایه‌ی پیک از تعرفه‌ی ناحیه‌ی آدرس (locations.DeliveryZone) می‌آید و
    # پست، پس‌کرایه است (orders/shipping.py). فقط کد ردیفِ کرایه‌ی پیک در فاکتور هلو اینجا می‌ماند.
    shipping_erp_code = models.CharField(
        max_length=100, default='bBALNA1mckd7Zh4O', verbose_name='ErpCode ردیف کرایه‌ی پیک در هلو',
        help_text='ErpCode کالای خدماتی هزینه ارسال (در هلو: «سرويس») که هنگام ثبت فاکتور برای سفارش‌های ارسال با پیک '
                  '(با کرایه‌ی بیشتر از صفر) به‌عنوان یک ردیف اضافه می‌شود. باید ErpCode واقعیِ همان کالا در هلو باشد، نه «کد کالا».',
    )
    # سرفصل «کارتخوان» (حساب بانکی دارای POS) که فاکتورِ پرداخت‌شده‌ی آنلاین/کیف‌پول/ترکیبی در هلو با آن تسویه می‌شود.
    # هلو سرفصل غیر-POS را رد می‌کند (خطای ۳۲). فعلاً همه‌ی پرداخت‌های آنلاین یک سرفصل دارند؛ تفکیک کیف‌پول بعداً.
    holoo_pos_sarfasl = models.CharField(
        max_length=30, default='10200010004', verbose_name='سرفصل کارتخوانِ پرداخت آنلاین در هلو',
        help_text='سرفصل حساب بانکیِ دارای کارتخوان در هلو که فاکتور سفارش‌های پرداخت‌شده با آن تسویه می‌شود.',
    )

    # --- موجودی و رزرو ---
    STOCK_SAFETY_BUFFER_CHOICES = ((0, 'بدون بافر'), (1, '۱ عدد'), (2, '۲ عدد'))
    stock_safety_buffer = models.PositiveSmallIntegerField(
        choices=STOCK_SAFETY_BUFFER_CHOICES, default=0, verbose_name='بافر اطمینان موجودی',
        help_text='این تعداد از موجودی هر کالا برای فروش آنلاین نگه داشته می‌شود (مثلاً برای فروش حضوری هم‌زمان در فروشگاه). '
                  'با بافر ۱، کالایی که فقط ۱ عدد موجودی دارد در سایت «ناموجود» نمایش داده می‌شود. ۰ = بدون بافر.',
    )

    # --- سیاست هزینه‌ی حمل (کرایه‌ی پیک درون‌شهری بر اساس ناحیه، پس‌کرایه‌ی پست برای بقیه‌ی شهرها) ---
    courier_free_for_free_shipping_cart = models.BooleanField(
        default=True, verbose_name='کرایه‌ی پیک هم برای سبدِ «ارسال رایگان» صفر شود',
        help_text='روشن: اگر همه‌ی کالاهای سبد «ارسال رایگان» باشند، کرایه‌ی پیک درون‌شهری هم صفر می‌شود. '
                  'خاموش: برچسب «ارسال رایگان» فقط روی پس‌کرایه اثر دارد و کرایه‌ی پیک ناحیه به قوت خود باقی می‌ماند.',
    )
    postage_collect_enabled = models.BooleanField(
        default=True, verbose_name='ارسال با پست (پس‌کرایه) فعال باشد',
        help_text='خاموش: به شهرهایی که ناحیه‌ی پیک ندارند فعلاً ارسال انجام نمی‌شود و کاربر پیام زیر را می‌بیند.',
    )
    postage_collect_label = models.CharField(
        max_length=200, default='پس‌کرایه (پرداخت هزینه درب منزل)', verbose_name='متن پس‌کرایه',
        help_text='این متن به‌جای مبلغ در فاکتور و صفحه‌ی تسویه‌حساب نمایش داده می‌شود؛ مبلغی به فاکتور اضافه نمی‌شود '
                  'و ردیف کرایه‌ای هم به هلو فرستاده نمی‌شود.',
    )
    postage_disabled_message = models.CharField(
        max_length=200, default='امکان ارسال به این شهر فعلاً وجود ندارد.', verbose_name='پیام غیرفعال بودن پس‌کرایه',
        help_text='وقتی «ارسال با پست» خاموش است و کاربر آدرسی خارج از نواحی پیک انتخاب می‌کند نمایش داده می‌شود.',
    )

    # --- قیمت برای کاربران مهمان (لاگین‌نکرده) ---
    # منطق اعمال در products/pricing.py است (تنها منبع قیمت)؛ اینجا فقط تنظیمات ادمین نگه‌داری می‌شود.
    # ترتیب محاسبه: تعیین سطح پایه ← تعدیل (فقط حالت فرمولی) ← تخفیف‌های خودکار. پیش‌فرض‌ها همان رفتار قبلی‌اند
    # (مهمان = قیمت سطح ۱)، پس با مایگریشن چیزی در سایت عوض نمی‌شود.
    guest_pricing_mode = models.CharField(
        max_length=20, choices=GUEST_PRICING_MODES, default=GUEST_PRICE_LEVEL,
        verbose_name='حالت نمایش قیمت برای کاربر مهمان',
        help_text='مهمان نمی‌تواند سفارش بدهد؛ این تنظیم فقط تعیین می‌کند چه قیمتی ببیند. در حالت «مخفی‌سازی» هیچ قیمتی '
                  '(نه قیمت اصلی، نه خط‌خورده، نه مبلغ تخفیف) به مهمان نشان داده نمی‌شود؛ فقط درصد تخفیف.',
    )
    guest_price_level = models.PositiveSmallIntegerField(
        choices=price_level_choices(), default=1, verbose_name='سطح قیمت مبنا برای مهمان',
        help_text='در حالت «یکی از قیمت‌های ده‌گانه» همین سطح نمایش داده می‌شود و در حالت «قیمت فرمولی» مبنای تعدیل است. '
                  'اگر قیمت این سطح در هلو صفر باشد، مثل بقیه‌ی کاربران قیمت سطح ۱ (چکی) نشان داده می‌شود. سطح ۳ تا ۱۰ '
                  'مثل کاربر ویژه حساب می‌شود و تخفیف خودکار نمی‌گیرد، مگر گزینه‌ی «روی قیمت ویژه هم اعمال شود» در «سیاست تخفیف» روشن باشد.',
    )
    guest_adjustment_type = models.CharField(
        max_length=10, choices=ADJUSTMENT_TYPES, default=ADJUST_PERCENT, verbose_name='نوع تعدیل قیمت مهمان',
        help_text='فقط در حالت «قیمت فرمولی».',
    )
    guest_adjustment_value = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, verbose_name='مقدار تعدیل قیمت مهمان',
        help_text='مثبت = افزایش، منفی = کاهش. درصدی: بین ‎-۹۰ تا ‎+۵۰۰ (مثلاً ‎15 یعنی ۱۵٪ گران‌تر از سطح مبنا). مبلغ ثابت: '
                  'عدد صحیح به تومان (مثلاً ‎50000 یا ‎-20000). در حالت «قیمت فرمولی» نباید صفر باشد.',
    )
    guest_price_rounding_step = models.PositiveIntegerField(
        choices=GUEST_ROUNDING_STEPS, default=1, verbose_name='گردکردن قیمت فرمولی مهمان',
        help_text='قیمت حاصل از فرمول به نزدیک‌ترین مضرب این مقدار گرد می‌شود (فقط حالت «قیمت فرمولی»).',
    )
    guest_price_hidden_message = models.CharField(
        max_length=200, default=GUEST_HIDDEN_MESSAGE_DEFAULT, verbose_name='متن راهنما به‌جای قیمت (مخفی‌سازی)',
        help_text='در حالت «مخفی‌سازی قیمت»، در کارت و صفحه‌ی محصول کنار دکمه‌ی «ورود / ثبت‌نام» نشان داده می‌شود.',
    )

    # کانالی که notifications.service از آن برای ارسال پیامک/اطلاع‌رسانی استفاده می‌کند.
    # این لیست دستی است چون هر بک‌اند تنظیمات خاص خودش را در settings.py می‌خواهد
    # (KAVENEGAR_API_KEY، MELIPAYAMAK_USERNAME/APIKEY)؛ افزودن بک‌اند جدید یعنی یک گزینه
    # اینجا و یک فایل در notifications/backends/.
    NOTIFICATION_BACKEND_CHOICES = (
        ('notifications.backends.console.ConsoleBackend', 'کنسول (فقط توسعه — چیزی واقعاً ارسال نمی‌شود)'),
        ('notifications.backends.kavenegar.KavenegarBackend', 'کاوه‌نگار'),
        ('notifications.backends.melipayamak.MelipayamakBackend', 'ملی‌پیامک (خط خدماتی اشتراکی)'),
    )
    notification_backend = models.CharField(
        max_length=190, choices=NOTIFICATION_BACKEND_CHOICES,
        default='notifications.backends.console.ConsoleBackend',
        verbose_name='سرویس ارسال پیامک/اطلاع‌رسانی',
        help_text='تعویض این گزینه فوری اثر می‌کند، بدون نیاز به تغییر کد یا ری‌استارت سرور.',
    )

    cart_fly_animation_enabled = models.BooleanField(
        default=True, verbose_name='افکت پرواز محصول به سبد خرید',
        help_text='با زدن «افزودن به سبد»، عکس محصول از کارت بیرون می‌آید، داخل یک سبد می‌افتد و سبد به آیکون سبد خرید '
                  'می‌رود و پنجره‌ی کوچک سبد چند ثانیه باز می‌ماند. خاموش = رفتار ساده‌ی قبلی (فقط شمارنده عوض می‌شود).',
    )

    # ------------------------------------------------------------------ گفتگوی آنلاین (فاز ۱: ظاهر و پیکربندی ویجت)
    # همه‌ی مقدارهای این بخش از تب «گفتگوی آنلاین» در ادمین عوض می‌شوند؛ اعتبارسنجی و منطق خالص در products/chat_settings.py.
    # فیلدهای مربوط به فازهای بعد (تایمرها، پیوست، پولینگ، پیامک، نگهداری) از همین حالا هستند تا مایگریشن یک‌بار انجام شود.
    CHAT_AI_HIDDEN = 'hidden'
    CHAT_AI_COMING_SOON = 'coming_soon'
    CHAT_AI_MODE_CHOICES = ((CHAT_AI_HIDDEN, 'مخفی‌سازی کامل'), (CHAT_AI_COMING_SOON, 'نمایش با نشان «به‌زودی»'))
    CHAT_POSITION_CHOICES = (('left', 'پایین چپ'), ('right', 'پایین راست'))
    CHAT_AVATAR_CHOICES = (
        ('support', 'پشتیبان خندان با هدست'), ('character', 'کاراکتر تمام‌قد با دست‌تکان‌دادن'), ('custom', 'تصویر اختصاصی (آپلود)'),
    )
    CHAT_FIELD_MODE_CHOICES = (('hidden', 'نمایش داده نشود'), ('optional', 'اختیاری'), ('required', 'اجباری'))
    CHAT_ATTACHMENT_MODE_CHOICES = (('images', 'فقط تصاویر (JPG/PNG/WebP)'), ('images_docs', 'تصاویر + PDF'))
    CHAT_HOURS_MODE_CHOICES = (('always', 'همیشه (۲۴ ساعته)'), ('by_schedule', 'طبق برنامه‌ی هفتگی و تعطیلات'))

    chat_enabled = models.BooleanField(
        default=False, verbose_name='فعال بودن گفتگوی آنلاین (کلید اصلی)',
        help_text='خاموش = هیچ ویجتی در سایت نمی‌آید. پیام آفلاین و پاسخ کارشناس فعال است؛ گفتگوی زنده در مرحله‌ی بعد اضافه می‌شود.')
    chat_tab_live_enabled = models.BooleanField(default=True, verbose_name='زبانه‌ی «گفتگوی آنلاین»')
    chat_tab_offline_enabled = models.BooleanField(default=True, verbose_name='زبانه‌ی «پیام آفلاین»')
    chat_ai_tab_mode = models.CharField(
        max_length=12, choices=CHAT_AI_MODE_CHOICES, default=CHAT_AI_COMING_SOON, verbose_name='زبانه‌ی «چت هوشمند»',
        help_text='فعلاً دستیار هوشمند ساخته نشده؛ یا با نشان «به‌زودی» دیده می‌شود یا کاملاً پنهان است.')
    chat_visible_for_guests = models.BooleanField(default=True, verbose_name='نمایش برای مهمان‌ها (لاگین‌نکرده)')
    chat_visible_for_users = models.BooleanField(default=True, verbose_name='نمایش برای کاربران واردشده')

    chat_position = models.CharField(max_length=5, choices=CHAT_POSITION_CHOICES, default='left', verbose_name='سمت دکمه‌ی شناور')
    chat_offset_x_px = models.PositiveSmallIntegerField(
        default=20, validators=[MaxValueValidator(200)], verbose_name='فاصله از لبه‌ی کناری - دسکتاپ (پیکسل)')
    chat_offset_y_px = models.PositiveSmallIntegerField(
        default=20, validators=[MaxValueValidator(200)], verbose_name='فاصله از پایین صفحه - دسکتاپ (پیکسل)')
    chat_offset_x_px_mobile = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MaxValueValidator(200)], verbose_name='فاصله از لبه‌ی کناری - موبایل (پیکسل)',
        help_text='خالی = خودکار (همان فاصله‌ی دسکتاپ).')
    chat_offset_y_px_mobile = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MaxValueValidator(200)], verbose_name='فاصله از پایین صفحه - موبایل (پیکسل)',
        help_text='خالی = خودکار: بالای منوی پایین موبایل می‌نشیند و روی آن نمی‌افتد.')
    chat_excluded_paths = models.TextField(
        blank=True, default='/orders/checkout/*\n/payments/*', verbose_name='صفحات مستثنی (ویجت در این‌ها لود نشود)',
        help_text='هر خط یک الگو: «/orders/checkout/» مسیر دقیق، «/payments/*» همه‌ی مسیرهای زیرمجموعه، «name:orders:checkout» نام URL. '
                  'خط‌هایی که با # شروع شوند نادیده‌اند. در صفحه‌ی مستثنی هیچ کد و اسکریپتی از چت بارگذاری نمی‌شود.')

    chat_avatar_choice = models.CharField(max_length=10, choices=CHAT_AVATAR_CHOICES, default='support', verbose_name='آواتار دکمه‌ی شناور')
    chat_avatar_custom = models.ImageField(
        upload_to='chat/avatar/', blank=True, null=True, verbose_name='آواتار اختصاصی',
        validators=[FileExtensionValidator(['jpg', 'jpeg', 'png', 'webp'])],
        help_text='فقط وقتی «تصویر اختصاصی» انتخاب شده استفاده می‌شود. JPG/PNG/WebP، ترجیحاً مربع (SVG پذیرفته نمی‌شود).')
    chat_primary_color = models.CharField(
        max_length=7, default='#FF8229', verbose_name='رنگ اصلی ویجت',
        validators=[RegexValidator(r'^#[0-9A-Fa-f]{6}$', 'رنگ باید کد Hex شش‌رقمی مثل #FF8229 باشد.')])

    chat_title = models.CharField(max_length=60, default='پشتیبانی بازرگانی موسوی', verbose_name='عنوان پنجره')
    chat_subtitle_online = models.CharField(max_length=120, default='آنلاین؛ معمولاً در چند دقیقه پاسخ می‌دهیم', verbose_name='زیرعنوان (آنلاین)')
    chat_subtitle_offline = models.CharField(max_length=120, default='کارشناسان در دسترس نیستند؛ پیام بگذارید', verbose_name='زیرعنوان (آفلاین)')
    chat_tab_live_label = models.CharField(max_length=24, default='گفتگوی آنلاین', verbose_name='برچسب زبانه‌ی گفتگوی آنلاین')
    chat_tab_offline_label = models.CharField(max_length=24, default='پیام آفلاین', verbose_name='برچسب زبانه‌ی پیام آفلاین')
    chat_tab_ai_label = models.CharField(max_length=24, default='چت هوشمند', verbose_name='برچسب زبانه‌ی چت هوشمند')
    chat_welcome_message = models.CharField(
        max_length=240, default='سلام! به بازرگانی موسوی خوش آمدید. چطور می‌توانیم کمکتان کنیم؟', verbose_name='پیام خوشامد')
    chat_msg_no_operator = models.TextField(
        default='در حال حاضر کارشناس آنلاینی در دسترس نیست. پیام خود را بگذارید؛ پاسخ را در همین ویجت و (برای کاربران واردشده) با پیامک دریافت می‌کنید.',
        verbose_name='پیام «کارشناس در دسترس نیست» (ساعت کاری)')
    chat_msg_after_hours = models.TextField(
        default='اکنون خارج از ساعت پاسخگویی هستیم. پیام خود را بگذارید تا در اولین فرصت پاسخ دهیم.', verbose_name='پیام «خارج از ساعت کاری»')
    chat_offline_form_intro = models.CharField(
        max_length=240, default='پیام خود را بنویسید؛ کارشناسان ما در اولین فرصت پاسخ می‌دهند.', verbose_name='مقدمه‌ی فرم پیام آفلاین')
    chat_offline_success_message = models.CharField(
        max_length=240, default='پیام شما ثبت شد. پاسخ را همین‌جا و (برای کاربران واردشده) با پیامک دریافت می‌کنید.',
        verbose_name='پیام موفقیت ثبت پیام آفلاین')
    chat_ai_coming_soon_text = models.CharField(
        max_length=240, default='دستیار هوشمند فروشگاه به‌زودی در خدمت شماست.', verbose_name='متن زبانه‌ی «چت هوشمند» (به‌زودی)')
    chat_privacy_notice = models.CharField(
        max_length=240, blank=True, default='', verbose_name='یادداشت حریم خصوصی (زیر فرم)',
        help_text='مثلاً «با ارسال پیام، با ذخیره‌ی گفتگو برای پشتیبانی موافقت می‌کنید.» خالی = نمایش داده نمی‌شود.')
    chat_message_placeholder = models.CharField(max_length=60, default='پیام خود را بنویسید…', verbose_name='متن راهنمای کادر پیام')
    chat_name_placeholder = models.CharField(max_length=40, default='نام شما', verbose_name='متن راهنمای کادر نام')
    chat_phone_placeholder = models.CharField(max_length=40, default='شماره موبایل (09123456789)', verbose_name='متن راهنمای کادر موبایل')
    chat_send_label = models.CharField(max_length=24, default='ارسال پیام', verbose_name='برچسب دکمه‌ی ارسال')
    chat_to_offline_label = models.CharField(max_length=40, default='ارسال پیام آفلاین', verbose_name='برچسب دکمه‌ی رفتن به فرم آفلاین')
    chat_close_conversation_label = models.CharField(max_length=40, default='پایان گفتگو', verbose_name='برچسب دکمه‌ی پایان گفتگو')

    chat_anim_enabled = models.BooleanField(
        default=True, verbose_name='انیمیشن‌های ویجت (خاموش کردن همه)',
        help_text='خاموش = هیچ حرکتی (شناوری، نبض، دست‌تکان‌دادن، حباب متحرک) اجرا نمی‌شود؛ بر همه‌ی گزینه‌های زیر غلبه دارد.')
    chat_anim_float = models.BooleanField(default=True, verbose_name='شناوری ملایم دکمه')
    chat_anim_pulse = models.BooleanField(default=True, verbose_name='نبض (حلقه‌ی جلب‌توجه)')
    chat_anim_wave = models.BooleanField(default=True, verbose_name='دست‌تکان‌دادن کاراکتر (فقط آواتار کاراکتر)')
    chat_anim_bubble = models.BooleanField(default=True, verbose_name='حباب پیام متحرک کنار دکمه')
    chat_bubble_messages = models.TextField(
        blank=True, default='سلام! کمکی از من برمیاد؟\nسؤالی درباره‌ی قیمت یا سفارش دارید؟\nپیام بگذارید، پاسخ می‌دهیم.',
        verbose_name='متن‌های حباب (هر خط یکی، به‌ترتیب می‌چرخند)', help_text='حداکثر ۲۰ خط و هر خط ۱۲۰ نویسه. خالی = حباب نمایش داده نمی‌شود.')
    chat_bubble_interval_seconds = models.PositiveSmallIntegerField(
        default=8, validators=[MinValueValidator(3), MaxValueValidator(120)], verbose_name='فاصله‌ی تعویض حباب (ثانیه)')
    chat_bubble_first_delay_seconds = models.PositiveSmallIntegerField(
        default=4, validators=[MaxValueValidator(60)], verbose_name='تأخیر نمایش اولین حباب (ثانیه)')
    chat_attention_interval_seconds = models.PositiveSmallIntegerField(
        default=20, validators=[MinValueValidator(5), MaxValueValidator(300)], verbose_name='فاصله‌ی جلب‌توجه (نبض و دست‌تکان‌دادن) (ثانیه)',
        help_text='نبض و دست‌تکان‌دادن حلقه‌ی بی‌پایان نیستند؛ هر چند ثانیه یک بار حدود ۳ ثانیه اجرا می‌شوند.')
    chat_respect_reduced_motion = models.BooleanField(
        default=True, verbose_name='رعایت «کاهش حرکت» سیستم‌عامل',
        help_text='روشن (پیشنهادی): کاربری که در سیستمش انیمیشن را خاموش کرده هیچ حرکتی نمی‌بیند. ')
    chat_launcher_dismiss_hours = models.PositiveSmallIntegerField(
        default=24, validators=[MaxValueValidator(720)], verbose_name='مدت مخفی ماندن بعد از بستن دکمه (ساعت)',
        help_text='کاربر با ضربدر کوچک دکمه را می‌بندد؛ این مدت در مرورگر خودش یادآوری می‌شود. ۰ = فقط تا بستن صفحه.')

    chat_guest_name_mode = models.CharField(max_length=8, choices=CHAT_FIELD_MODE_CHOICES, default='optional', verbose_name='کادر نام (مهمان)')
    chat_guest_phone_mode = models.CharField(max_length=8, choices=CHAT_FIELD_MODE_CHOICES, default='optional', verbose_name='کادر موبایل (مهمان)')
    chat_message_max_length = models.PositiveSmallIntegerField(
        default=1000, validators=[MinValueValidator(50), MaxValueValidator(4000)], verbose_name='حداکثر طول هر پیام (نویسه)')
    chat_rate_limit_per_minute = models.PositiveSmallIntegerField(
        default=10, validators=[MinValueValidator(1), MaxValueValidator(60)], verbose_name='سقف تعداد پیام در دقیقه (هر بازدیدکننده)')
    chat_guest_max_conversations_per_day = models.PositiveSmallIntegerField(
        default=5, validators=[MinValueValidator(1), MaxValueValidator(50)], verbose_name='سقف گفتگوی جدید در روز (هر IP)')
    chat_captcha_after_n_conversations = models.PositiveSmallIntegerField(
        default=2, validators=[MaxValueValidator(20)], verbose_name='کپچا بعد از چند گفتگو (۰ = هرگز)')
    chat_retention_days = models.PositiveSmallIntegerField(
        default=0, validators=[MaxValueValidator(3650)], verbose_name='نگهداری گفتگوها (روز)', help_text='۰ = برای همیشه نگه داشته شود.')

    chat_attachments_enabled = models.BooleanField(default=False, verbose_name='ارسال پیوست در گفتگو')
    chat_attachments_mode = models.CharField(max_length=12, choices=CHAT_ATTACHMENT_MODE_CHOICES, default='images', verbose_name='نوع فایل‌های مجاز')
    chat_attachment_max_mb = models.PositiveSmallIntegerField(
        default=5, validators=[MinValueValidator(1), MaxValueValidator(20)], verbose_name='حداکثر حجم هر فایل (مگابایت)')
    chat_attachment_max_count = models.PositiveSmallIntegerField(
        default=3, validators=[MinValueValidator(1), MaxValueValidator(5)], verbose_name='حداکثر تعداد فایل در هر پیام')

    chat_operator_timeout_seconds = models.PositiveSmallIntegerField(
        default=60, validators=[MinValueValidator(20), MaxValueValidator(600)], verbose_name='مهلت نبض کارشناس برای «آنلاین» ماندن (ثانیه)')
    chat_live_requires_operator = models.BooleanField(
        default=True, verbose_name='گفتگوی زنده فقط وقتی کارشناس آنلاین است',
        help_text='خاموش = در ساعت کاری، گفتگوی زنده حتی بدون کارشناس آنلاین هم شروع می‌شود و در صف می‌ماند.')
    chat_poll_active_seconds = models.PositiveSmallIntegerField(
        default=3, validators=[MinValueValidator(2), MaxValueValidator(30)], verbose_name='فاصله‌ی به‌روزرسانی - پنجره‌ی باز (ثانیه)')
    chat_poll_idle_seconds = models.PositiveSmallIntegerField(
        default=10, validators=[MinValueValidator(2), MaxValueValidator(120)], verbose_name='فاصله‌ی به‌روزرسانی - کاربر بی‌فعالیت (ثانیه)')
    chat_poll_closed_seconds = models.PositiveSmallIntegerField(
        default=60, validators=[MaxValueValidator(600)], verbose_name='فاصله‌ی به‌روزرسانی - پنجره‌ی بسته (ثانیه)',
        help_text='۰ = وقتی پنجره بسته است اصلاً درخواست نمی‌فرستد؛ وگرنه بین ۱۵ تا ۶۰۰.')

    chat_operator_response_sla_minutes = models.PositiveSmallIntegerField(
        default=10, validators=[MaxValueValidator(240)], verbose_name='مهلت پاسخ کارشناس (دقیقه)',
        help_text='بعد از این مدت بدون پاسخ، گفتگو به حالت «آفلاین (ناهمزمان)» می‌رود. ۰ = غیرفعال.')
    chat_customer_idle_minutes = models.PositiveSmallIntegerField(
        default=10, validators=[MinValueValidator(1), MaxValueValidator(240)], verbose_name='بی‌پاسخی مشتری تا «منتظر مشتری» (دقیقه)')
    chat_customer_gone_minutes = models.PositiveSmallIntegerField(
        default=30, validators=[MinValueValidator(5), MaxValueValidator(1440)], verbose_name='رفتن مشتری تا حالت آفلاین (دقیقه)')
    chat_continuity_minutes = models.PositiveSmallIntegerField(
        default=15, validators=[MinValueValidator(1), MaxValueValidator(240)], verbose_name='پنجره‌ی پیوستگی گفتگو (دقیقه)')
    chat_assignee_timeout_minutes = models.PositiveSmallIntegerField(
        default=15, validators=[MinValueValidator(1), MaxValueValidator(240)], verbose_name='غیبت پیوسته‌ی کارشناس مسئول تا بازگشت به صف (دقیقه)')
    chat_idle_close_hours = models.PositiveSmallIntegerField(
        default=48, validators=[MinValueValidator(1), MaxValueValidator(720)], verbose_name='بستن خودکار گفتگوی بی‌فعالیت (ساعت)')
    chat_reopen_window_hours = models.PositiveSmallIntegerField(
        default=72, validators=[MinValueValidator(1), MaxValueValidator(720)], verbose_name='مهلت بازگشایی گفتگوی بسته (ساعت)')
    chat_reopen_on_customer_message = models.BooleanField(default=True, verbose_name='پیام مشتری گفتگوی بسته را بازگشایی کند')

    chat_timezone = models.CharField(
        max_length=40, default='Asia/Tehran', verbose_name='منطقه‌ی زمانی چت',
        help_text='نام استاندارد IANA، مثل Asia/Tehran. ساعات کاری و تعطیلات بر پایه‌ی همین ساعت محاسبه می‌شوند.')
    chat_hours_mode = models.CharField(max_length=12, choices=CHAT_HOURS_MODE_CHOICES, default='by_schedule', verbose_name='حالت ساعات کاری')
    chat_hours_sat = models.CharField(max_length=80, blank=True, default='08:00-12:00, 13:00-17:00', verbose_name='ساعات شنبه',
                                      help_text='چند بازه با ویرگول؛ مثل 08:00-12:00, 13:00-17:00. خالی = تعطیل.')
    chat_hours_sun = models.CharField(max_length=80, blank=True, default='08:00-12:00, 13:00-17:00', verbose_name='ساعات یکشنبه')
    chat_hours_mon = models.CharField(max_length=80, blank=True, default='08:00-12:00, 13:00-17:00', verbose_name='ساعات دوشنبه')
    chat_hours_tue = models.CharField(max_length=80, blank=True, default='08:00-12:00, 13:00-17:00', verbose_name='ساعات سه‌شنبه')
    chat_hours_wed = models.CharField(max_length=80, blank=True, default='08:00-12:00, 13:00-17:00', verbose_name='ساعات چهارشنبه')
    chat_hours_thu = models.CharField(max_length=80, blank=True, default='', verbose_name='ساعات پنجشنبه')
    chat_hours_fri = models.CharField(max_length=80, blank=True, default='', verbose_name='ساعات جمعه')
    chat_holidays = models.TextField(
        blank=True, default='', verbose_name='روزهای تعطیل (تاریخ جلالی)',
        help_text='هر خط یک روز: «1405/07/21 عنوان اختیاری». در این روزها همیشه «خارج از ساعت کاری» است.')

    chat_admin_sms_cooldown_minutes = models.PositiveSmallIntegerField(
        default=10, validators=[MinValueValidator(1), MaxValueValidator(1440)], verbose_name='فاصله‌ی حداقلی پیامک به کارشناس (دقیقه)')
    chat_customer_sms_cooldown_minutes = models.PositiveSmallIntegerField(
        default=30, validators=[MinValueValidator(1), MaxValueValidator(1440)], verbose_name='فاصله‌ی حداقلی پیامک به مشتری (دقیقه)')
    chat_notify_phones = models.TextField(
        blank=True, default='', verbose_name='شماره‌های کارشناسان برای پیامک (هر خط یکی)',
        help_text='خالی = همان شماره‌ی پیش‌فرض اعلان مدیر. روشن/خاموش بودن و متن پیامک‌ها در «اطلاع‌رسانی ← تنظیمات انواع پیام» است.')

    cart_hover_popup_enabled = models.BooleanField(
        default=True, verbose_name='باز شدن پنجره‌ی سبد با رفتن ماوس روی آیکون سبد (دسکتاپ)',
        help_text='کاربر دسکتاپ با نگه داشتن ماوس روی آیکون سبد خرید هدر، همان پنجره‌ی کوچک سبد را می‌بیند (عکس کالاها، تعداد، جمع کل). '
                  'روی موبایل/لمسی اثری ندارد. کلیک روی آیکون همچنان کشوی کامل سبد را باز می‌کند. فقط برای کاربر واردشده.',
    )
    cart_fly_respect_reduced_motion = models.BooleanField(
        default=False, verbose_name='رعایت «کاهش حرکت» سیستم‌عامل در افکت سبد',
        help_text='اگر روشن باشد و کاربر در سیستم‌عاملش انیمیشن‌ها را خاموش کرده باشد (ویندوز: تنظیمات ← دسترسی‌پذیری ← جلوه‌های '
                  'بصری)، فقط پنجره‌ی سبد نشان داده می‌شود و عکس پرواز نمی‌کند. پیش‌فرض خاموش است چون افکت کوتاه است و فقط با '
                  'کلیک خودِ کاربر اجرا می‌شود؛ روی بسیاری از رایانه‌ها (از جمله سرورهای ویندوز و ریموت دسکتاپ) این گزینه‌ی سیستم '
                  'ناخواسته فعال است و افکت دیده نمی‌شد.',
    )

    show_stories = models.BooleanField(default=True, verbose_name='نمایش بخش استوری در صفحه اصلی')
    show_hero_slider = models.BooleanField(default=True, verbose_name='نمایش اسلایدر اصلی در صفحه اصلی')
    show_amazing_deal = models.BooleanField(default=True, verbose_name='نمایش بخش تخفیف‌دارها / شگفت‌انگیز')
    show_best_selling = models.BooleanField(default=True, verbose_name='نمایش بخش پرفروش‌ترین‌ها')
    show_blog_posts = models.BooleanField(default=True, verbose_name='نمایش بخش مقالات و وبلاگ')

    # --- برندسازی: لوگو/فاوآیکون سفارشی (اختیاری) ---
    # هر دو اختیاری‌اند؛ وقتی خالی باشند تمپلیت‌ها همان فایل‌های استاتیک پیش‌فرض تم را نشان
    # می‌دهند (نگاه کنید partials/site_logo.html و base.html) - یعنی جایگزینی این فیلدها
    # هرگز چیزی را روی سایت خراب نمی‌کند، فقط در صورت آپلود جایگزین می‌شود.
    logo_image = models.ImageField(
        upload_to='branding/', blank=True, verbose_name='لوگوی سفارشی سایت',
        help_text='جایگزین لوگوی پیش‌فرض تم در هدر/فوتر (حالت روشن). خالی = همان لوگوی پیش‌فرض.',
    )
    favicon_image = models.ImageField(
        upload_to='branding/', blank=True, verbose_name='فاوآیکون سفارشی',
        help_text='جایگزین فاوآیکون پیش‌فرض تم. خالی = همان فاوآیکون پیش‌فرض.',
    )
    login_hero_image = models.ImageField(
        upload_to='branding/', blank=True, verbose_name='تصویر بزرگ صفحه‌ی ورود',
        help_text='تصویر پس‌زمینه‌ی سمت راست صفحه‌ی ورود/ثبت‌نام (فقط در دسکتاپ نمایش داده می‌شود). خالی = تصویر پیش‌فرض تم.',
    )
    login_banner_title = models.CharField(
        max_length=200, blank=True, default='به فروشگاه هلو خوش آمدید', verbose_name='عنوان روی بنر صفحه‌ی ورود',
    )
    login_banner_subtitle = models.CharField(
        max_length=300, blank=True,
        default='با وارد کردن شماره موبایل خود، به سرعت وارد حساب کاربری شوید یا ثبت‌نام کنید.',
        verbose_name='زیرعنوان روی بنر صفحه‌ی ورود',
    )
    login_badge_icon = models.ImageField(
        upload_to='branding/', blank=True, verbose_name='آیکون نشان بالای فرم ورود',
        help_text='جایگزین آیکون Shield پیش‌فرض بالای فرم ورود (هم صفحه‌ی کامل هم پاپ‌آپ). خالی = همان آیکون پیش‌فرض.',
    )

    show_newsletter = models.BooleanField(default=True, verbose_name='نمایش عضویت در خبرنامه (فوتر)')
    show_app_download = models.BooleanField(default=True, verbose_name='نمایش دانلود اپلیکیشن (فوتر)')
    app_google_play_url = models.URLField(blank=True, verbose_name='لینک گوگل‌پلی')
    app_sibapp_url = models.URLField(blank=True, verbose_name='لینک سیب‌اپ')
    app_bazaar_url = models.URLField(blank=True, verbose_name='لینک کافه‌بازار')
    app_myket_url = models.URLField(blank=True, verbose_name='لینک مایکت')
    app_direct_download_url = models.URLField(blank=True, verbose_name='لینک دانلود مستقیم')

    # --- امتیاز و سطح وفاداری مشتریان ---
    # سطح از روی امتیاز تعیین می‌شود (نه مستقیم تعداد سفارش)؛ امتیاز هم از یکی از این دو فرمول
    # می‌آید. تعداد/ترتیب ۵ سطح (مشتری جدید تا الماسی) عمداً ثابت مانده - CustomUser.LOYALTY_LEVELS
    # و promotions.models.LOYALTY_CHOICES به همین تعداد/ترتیب وابسته‌اند - فقط آستانه‌های امتیاز
    # هر سطح این‌جا قابل‌تنظیم است. محاسبه‌ها زنده‌اند (accounts/stats.py:get_config -> products/stats.py)
    # پس با ذخیره‌ی این تنظیمات، سطح/امتیاز همه‌ی کاربران در همان درخواست بعدی به‌روز می‌شود.
    LOYALTY_MODE_ORDER_COUNT = 'order_count'
    LOYALTY_MODE_AMOUNT = 'amount'
    LOYALTY_MODE_CHOICES = [
        (LOYALTY_MODE_ORDER_COUNT, 'بر اساس تعداد سفارش'),
        (LOYALTY_MODE_AMOUNT, 'بر اساس مبلغ خرید'),
    ]
    loyalty_mode = models.CharField(
        max_length=15, choices=LOYALTY_MODE_CHOICES, default=LOYALTY_MODE_ORDER_COUNT,
        verbose_name='مبنای امتیازدهی',
    )
    loyalty_points_per_order = models.PositiveIntegerField(
        default=100, verbose_name='امتیاز هر سفارش موفق',
        help_text='فقط در حالت «بر اساس تعداد سفارش» استفاده می‌شود.',
    )
    loyalty_amount_step = models.PositiveIntegerField(
        default=100000, verbose_name='مبلغ هر ۱ امتیاز (تومان)',
        help_text='فقط در حالت «بر اساس مبلغ خرید». مبنا مبلغ خالص اقلام سفارش است (بعد از تخفیف خودکار '
                  'ردیف‌ها، بدون احتساب تخفیف کد سفارش و بدون هزینه‌ی ارسال).',
    )
    loyalty_threshold_bronze = models.PositiveIntegerField(default=300, verbose_name='آستانه‌ی سطح برنزی (امتیاز)')
    loyalty_threshold_silver = models.PositiveIntegerField(default=700, verbose_name='آستانه‌ی سطح نقره‌ای (امتیاز)')
    loyalty_threshold_gold = models.PositiveIntegerField(default=1500, verbose_name='آستانه‌ی سطح طلایی (امتیاز)')
    loyalty_threshold_diamond = models.PositiveIntegerField(default=3000, verbose_name='آستانه‌ی سطح الماسی (امتیاز)')
    # مرز فعال‌سازی باشگاه مشتریان (Loyalty Phase 2A) - خواندنش در فازهای بعدی، در لحظه‌ی کسب
    # امتیاز: سفارش‌های ثبت‌شده پیش از این زمان هرگز امتیاز نمی‌گیرند (بدون بک‌فیل). خالی/NULL
    # یعنی باشگاه هنوز فعال نشده - هیچ سفارشی (حتی تازه) امتیاز نمی‌گیرد. عمداً مستقل از
    # loyalty_mode/loyalty_threshold_* بالا: آن‌ها فرمول سطح زنده‌ی فعلی را تغذیه می‌کنند، این
    # فیلد فقط مصرف‌کننده‌اش موتور کسب/دفترکل فاز ۲ خواهد بود.
    loyalty_activated_at = models.DateTimeField(
        null=True, blank=True, verbose_name='تاریخ و زمان فعال‌سازی باشگاه مشتریان',
        help_text='در صورت خالی بودن، باشگاه مشتریان غیرفعال است و سفارشی امتیاز دریافت نمی‌کند.',
    )
    # Loyalty Phase 5B-2 - فلگ بازگشت‌پذیر برای اعمال رتبه‌ی داینامیک در صلاحیت تخفیف/کوپن؛
    # خواندنش loyalty/stats.py::effective_loyalty_index. پیش‌فرض False تا رفتار پروداکشن دست‌نخورده
    # بماند؛ خاموش‌کردن این فلگ = بازگشت فوری به ۱۰۰٪ سیستم سنتی، بدون دیپلوی/ری‌استارت
    # (همان ابطال کش لحظه‌ای save()ی خودِ SiteSettings).
    loyalty_dynamic_tier_in_eligibility = models.BooleanField(
        default=False, verbose_name='اعمال رتبه داینامیک باشگاه در صلاحیت تخفیف‌ها',
        help_text='در صورت فعال بودن، رتبه داینامیک باشگاه از طریق کراس‌واک صریح و سیاست سقف رتبه '
                  '(Higher-Of) روی تخفیف‌ها و کوپن‌ها اعمال می‌شود.',
    )

    # --- تبدیل امتیاز به کیف‌پول (Loyalty Phase 4A) - خواندنش loyalty/redemption.py؛ هر ۴ فیلد
    # ۱۰۰٪ از پنل قابل تنظیم‌اند، هیچ عدد ثابتی در کد ارکستریتور نیست.
    loyalty_redeem_toman_per_point = models.PositiveIntegerField(
        default=100, validators=[MinValueValidator(1)], verbose_name='نرخ تبدیل امتیاز به کیف‌پول (تومان به‌ازای هر امتیاز)',
        help_text='هر امتیاز باشگاه هنگام تبدیل به کیف‌پول معادل چند تومان شارژ می‌شود.',
    )
    loyalty_redeem_min_points = models.PositiveIntegerField(
        default=50, validators=[MinValueValidator(1)], verbose_name='حداقل امتیاز مجاز برای هر بار تبدیل',
    )
    loyalty_redeem_max_points_per_transaction = models.PositiveIntegerField(
        default=500, validators=[MinValueValidator(1)], verbose_name='حداکثر امتیاز مجاز در هر تراکنش تبدیل',
    )
    loyalty_redeem_max_points_per_day = models.PositiveIntegerField(
        default=1000, validators=[MinValueValidator(1)], verbose_name='حداکثر امتیاز مجاز تبدیل در روز برای هر کاربر',
        help_text='سقف امنیتی/مالی روزانه؛ باید حداقل برابر سقف تک‌تراکنش باشد.',
    )

    # --- مهلت مرجوعی کالا (Phase 1 - Part B.1) - خواندنش returns/deadline.py:is_order_within_return_window ---
    RETURN_PERIOD_UNIT_WORKING_DAYS = 'working_days'
    RETURN_PERIOD_UNIT_CALENDAR_DAYS = 'calendar_days'
    RETURN_PERIOD_UNIT_CHOICES = (
        (RETURN_PERIOD_UNIT_WORKING_DAYS, 'روز کاری'),
        (RETURN_PERIOD_UNIT_CALENDAR_DAYS, 'روز تقویمی'),
    )
    return_period_days = models.PositiveIntegerField(default=7, verbose_name='مهلت مرجوعی کالا')
    return_period_unit = models.CharField(
        max_length=20, choices=RETURN_PERIOD_UNIT_CHOICES, default=RETURN_PERIOD_UNIT_WORKING_DAYS,
        verbose_name='واحد محاسبه‌ی مهلت مرجوعی',
        help_text='روز کاری: جمعه‌ها شمرده نمی‌شوند. روز تقویمی: دقیقاً N×۲۴ ساعت از لحظه‌ی تحویل.',
    )
    # محتوای کامل صفحه‌ی «روش مرجوعی کالا» (جایگزین return-procedure.html هاردکدِ آرینو - Part C
    # همین‌جا را می‌خواند، نه یک تمپلیت ثابت)
    return_policy_html = CKEditor5Field(
        'متن کامل راهنما/قوانین مرجوعی کالا', blank=True, config_name='default',
        help_text='این متن عیناً در صفحه‌ی «روش مرجوعی کالا» به مشتری نمایش داده می‌شود.',
    )
    # سقف حجم مدارک مرجوعی (Phase 1 - Part C.3) - خواندنش returns/models.py:ReturnAttachment.clean؛
    # سقف *تعداد* فایل (۵ تا) عمداً این‌جا نیست، ثابت است (ReturnAttachment.MAX_PER_ITEM)
    return_attachment_max_image_mb = models.PositiveIntegerField(
        default=5, verbose_name='حداکثر حجم هر عکسِ مدرک مرجوعی (مگابایت)',
    )
    return_attachment_max_video_mb = models.PositiveIntegerField(
        default=50, verbose_name='حداکثر حجم هر ویدئوی مدرک مرجوعی (مگابایت)',
    )

    # --- چیدمان ظاهری فروشگاه (خواندنش templates/base.html برای عرض + static/theme/assets/js/
    # dependencies/app.js::applyViewMode برای پیش‌فرض تعداد ستون کارت محصول) ---
    site_content_max_width = models.PositiveIntegerField(
        default=1728, verbose_name='حداکثر عرض محتوای سایت (پیکسل)',
        help_text='عرض بخش اصلی صفحات فروشگاهی (هدر، فوتر، محتوا) روی صفحه‌نمایش‌های عریض؛ هرچه بیشتر باشد '
                  'فضای خالی کناره‌های چپ/راست صفحه کمتر می‌شود. مقدار پیش‌فرض قبلی قالب ۱۵۳۶ پیکسل بود. '
                  'این مقدار روی پنل کاربری اثر ندارد - نگاه کنید dashboard_content_max_width.',
    )
    DEFAULT_SHOP_COLUMNS_3 = 'grid-3'
    DEFAULT_SHOP_COLUMNS_4 = 'grid-4'
    DEFAULT_SHOP_COLUMNS_CHOICES = (
        (DEFAULT_SHOP_COLUMNS_3, '۳ ستونه'),
        (DEFAULT_SHOP_COLUMNS_4, '۴ ستونه'),
    )
    default_shop_columns = models.CharField(
        max_length=10, choices=DEFAULT_SHOP_COLUMNS_CHOICES, default=DEFAULT_SHOP_COLUMNS_4,
        verbose_name='چیدمان پیش‌فرض کارت‌های فروشگاه',
        help_text='کاربر همچنان می‌تواند از دکمه‌ی «نحوه نمایش» بالای صفحه‌ی فروشگاه بین ۳/۴ ستونه یا لیستی '
                  'جابه‌جا شود؛ این فقط پیش‌فرضِ اولین بازدید (پیش از ذخیره شدن ترجیح در مرورگر کاربر) را تعیین می‌کند.',
    )
    HERO_SLIDER_WIDTH_DYNAMIC = 'dynamic'
    HERO_SLIDER_WIDTH_FULL = 'full'
    HERO_SLIDER_WIDTH_MODE_CHOICES = (
        (HERO_SLIDER_WIDTH_DYNAMIC, 'متناسب با عرض سایت (هم‌عرض ستون اصلی محتوا)'),
        (HERO_SLIDER_WIDTH_FULL, 'تمام عرض (لبه‌به‌لبه، مستقل از عرض سایت)'),
    )
    hero_slider_width_mode = models.CharField(
        max_length=10, choices=HERO_SLIDER_WIDTH_MODE_CHOICES, default=HERO_SLIDER_WIDTH_DYNAMIC,
        verbose_name='عرض اسلایدر اصلی صفحه‌ی نخست',
        help_text='«متناسب با عرض سایت»: اسلایدر هم‌عرض ستون اصلی محتوا (site_content_max_width بالا) می‌ماند '
                  'و با تغییر آن رشد/کوچک می‌شود. «تمام عرض»: صرف‌نظر از عرض محتوای سایت، اسلایدر تمام پهنای '
                  'صفحه‌نمایش را می‌گیرد.',
    )
    # --- چیدمان ظاهری پنل کاربری (داشبورد مشتری) - عمداً از site_content_max_width بالا جداست تا
    # تغییر عرض صفحات فروشگاهی، ظاهر پنل کاربری (حساب کاربری/سفارش‌ها/باشگاه مشتریان) را به‌هم نریزد.
    # خواندنش templates/accounts/dashboard_base.html ---
    dashboard_content_max_width = models.PositiveIntegerField(
        default=1536, verbose_name='حداکثر عرض محتوای پنل کاربری (پیکسل)',
        help_text='عرض صفحات پنل کاربری (حساب کاربری، سفارش‌ها، باشگاه مشتریان و ...) - مستقل از «حداکثر عرض '
                  'محتوای سایت» که فقط صفحات فروشگاهی را کنترل می‌کند.',
    )

    # --- چیدمان صفحه‌ی مدیریت جنگو (/admin) - جنگو به‌طور پیش‌فرض هیچ حداکثر عرضی برای #container
    # ندارد (تمام‌عرض صفحه‌نمایش)؛ این فیلد کاملاً اختیاری است و فقط وقتی مقدار بگیرد صفحات ادمین
    # وسط‌چین و محدود می‌شوند. خالی = دقیقاً همان رفتار پیش‌فرض جنگو، بدون هیچ تغییری.
    # خواندنش templates/admin/base_site.html ---
    admin_panel_max_width = models.PositiveIntegerField(
        null=True, blank=True, verbose_name='حداکثر عرض صفحه‌ی مدیریت جنگو (پیکسل، اختیاری)',
        help_text='محدودکردن عرض صفحات /admin/ روی صفحه‌نمایش‌های عریض (صفحه وسط‌چین می‌شود). خالی = بدون '
                  'محدودیت، همان رفتار پیش‌فرض جنگو (تمام عرض). مستقل از عرض سایت و عرض پنل کاربری بالا.',
    )

    # --- تصاویر پیش‌فرض کارت محصولِ بدون عکس - خواندنش Product.main_image_url/hover_image_url.
    # هر دو اختیاری‌اند؛ خالی بودن هرکدام یعنی همان فایل استاتیک پیش‌فرض تم (Preload.webp /
    # Preload-2.webp) به‌جای آن استفاده می‌شود - یعنی آپلود این دو عکس هرگز چیزی را خراب نمی‌کند.
    no_image_1 = models.ImageField(
        upload_to='branding/', blank=True, verbose_name='تصویر پیش‌فرض محصول بدون عکس (نو ایمیج ۱)',
        help_text='وقتی محصولی هیچ تصویر اصلی‌ای از اسکنر عکس نگرفته باشد، به‌جای Preload.webp پیش‌فرض تم '
                  'همین عکس در کارت/صفحه‌ی محصول نمایش داده می‌شود. خالی = همان Preload.webp پیش‌فرض تم.',
    )
    no_image_2 = models.ImageField(
        upload_to='branding/', blank=True, verbose_name='تصویر پیش‌فرض محصول بدون عکس (نو ایمیج ۲ / هاور)',
        help_text='فقط برای محصولی که اصلاً هیچ عکسی (نه اصلی، نه گالری) ندارد؛ جلوه‌ی هاور روی کارت این '
                  'محصول به‌جای Preload-2.webp پیش‌فرض تم همین عکس را نشان می‌دهد. اگر محصول فقط یک عکس '
                  'اصلی داشته باشد (بدون گالری)، هاور به‌جای این عکس، همان عکس اصلی را زوم می‌کند. '
                  'خالی = همان Preload-2.webp پیش‌فرض تم.',
    )

    # --- مگامنوی دسته‌بندی‌های هدر (دسکتاپ) - خواندنش templates/base.html از طریق mega_menu_inline_style
    # (متغیرهای CSS روی خودِ پنل) و بلوک .mega-* در static/theme/assets/css/app.css. پیش‌فرض‌ها همان
    # ظاهر فعلی‌اند (عرض هم‌اندازه‌ی سایت، سفید، بدون شیشه/عکس پس‌زمینه). رنگ/شفافیت/بلور/عکس پس‌زمینه
    # فقط روی تم روشن اثر دارند؛ حالت تیره همیشه استایل پیش‌فرض خودش را دارد. ---
    MEGA_WIDTH_CONTAINER = 'container'
    MEGA_WIDTH_PX = 'px'
    MEGA_WIDTH_PERCENT = 'percent'
    MEGA_WIDTH_MODE_CHOICES = (
        (MEGA_WIDTH_CONTAINER, 'هم‌عرض محتوای سایت'),
        (MEGA_WIDTH_PX, 'عرض ثابت (پیکسل)'),
        (MEGA_WIDTH_PERCENT, 'درصدی از عرض هدر'),
    )
    mega_menu_width_mode = models.CharField(
        max_length=10, choices=MEGA_WIDTH_MODE_CHOICES, default=MEGA_WIDTH_CONTAINER,
        verbose_name='حالت عرض مگامنو',
    )
    mega_menu_width_value = models.PositiveIntegerField(
        default=1200, verbose_name='مقدار عرض مگامنو',
        help_text='فقط وقتی «حالت عرض» روی پیکسل (۶۰۰ تا ۲۵۶۰) یا درصد (۵۰ تا ۱۰۰) باشد اعمال می‌شود؛ '
                  'در حالت «هم‌عرض محتوای سایت» نادیده گرفته می‌شود.',
    )
    mega_menu_max_height = models.PositiveIntegerField(
        default=400, verbose_name='حداکثر ارتفاع مگامنو (پیکسل)',
        help_text='بین ۲۴۰ تا ۸۰۰. اگر محتوا بلندتر باشد، همان پنل اسکرول نرم اختصاصی می‌گیرد.',
    )
    MEGA_COLUMN_CHOICES = ((3, '۳ ستونه'), (4, '۴ ستونه'), (5, '۵ ستونه'))
    mega_menu_columns = models.PositiveSmallIntegerField(
        choices=MEGA_COLUMN_CHOICES, default=4, verbose_name='تعداد ستون زیردسته‌ها',
    )
    mega_menu_bg_color = models.CharField(
        max_length=7, default='#FFFFFF', verbose_name='رنگ پس‌زمینه‌ی مگامنو (تم روشن)',
        validators=[RegexValidator(r'^#[0-9A-Fa-f]{6}$', 'رنگ باید کد Hex شش‌رقمی مثل #FFFFFF باشد.')],
        help_text='پیش‌فرض سفید (همان ظاهر فعلی). فقط روی تم روشن اثر دارد.',
    )
    mega_menu_bg_opacity = models.PositiveSmallIntegerField(
        default=100, verbose_name='شفافیت پس‌زمینه‌ی مگامنو (درصد)',
        help_text='بین ۱۰ تا ۱۰۰؛ ۱۰۰ = کاملاً مات. فقط روی رنگ پس‌زمینه اثر دارد (نه روی عکس پس‌زمینه). '
                  'برای افکت شیشه‌ای مقدار کمتر از ۱۰۰ را با «بلور» ترکیب کنید.',
    )
    mega_menu_blur_px = models.PositiveSmallIntegerField(
        default=0, verbose_name='میزان بلور شیشه‌ای پشت مگامنو (پیکسل)',
        help_text='بین ۰ تا ۴۰؛ ۰ = بدون بلور. وقتی معنا دارد که شفافیت کمتر از ۱۰۰ باشد.',
    )
    mega_menu_bg_image = models.ImageField(
        upload_to='branding/', blank=True, verbose_name='تصویر پس‌زمینه‌ی مگامنو (تم روشن)',
        help_text='اختیاری؛ روی رنگ پس‌زمینه می‌نشیند. خالی = بدون تصویر.',
    )
    MEGA_BG_COVER = 'cover'
    MEGA_BG_REPEAT = 'repeat'
    MEGA_BG_IMAGE_MODE_CHOICES = (
        (MEGA_BG_COVER, 'پوشش کامل (cover)'),
        (MEGA_BG_REPEAT, 'تکرار (repeat)'),
    )
    mega_menu_bg_image_mode = models.CharField(
        max_length=10, choices=MEGA_BG_IMAGE_MODE_CHOICES, default=MEGA_BG_COVER,
        verbose_name='حالت تصویر پس‌زمینه‌ی مگامنو',
    )
    mega_menu_show_parent_images = models.BooleanField(
        default=True, verbose_name='نمایش تصویر دسته‌های اصلی (ستون راست مگامنو)',
        help_text='دسته‌ای که تصویر شاخص ندارد، کادر تصویرش کاملاً پنهان می‌شود.',
    )
    mega_menu_show_child_images = models.BooleanField(
        default=False, verbose_name='نمایش تصویر زیردسته‌ها',
        help_text='زیردسته‌ای که تصویر شاخص ندارد، کادر تصویرش کاملاً پنهان می‌شود (فضای خالی نمی‌ماند).',
    )
    MEGA_IMAGE_POSITION_START = 'start'
    MEGA_IMAGE_POSITION_END = 'end'
    MEGA_IMAGE_POSITION_CHOICES = (
        (MEGA_IMAGE_POSITION_START, 'سمت راست عنوان'),
        (MEGA_IMAGE_POSITION_END, 'سمت چپ عنوان'),
    )
    mega_menu_image_position = models.CharField(
        max_length=5, choices=MEGA_IMAGE_POSITION_CHOICES, default=MEGA_IMAGE_POSITION_START,
        verbose_name='موقعیت تصویر نسبت به عنوان دسته',
    )
    mega_menu_image_size = models.PositiveSmallIntegerField(
        default=32, verbose_name='اندازه‌ی تصویر دسته (پیکسل)', help_text='بین ۱۶ تا ۶۴؛ مثلاً ۲۴ یا ۳۲ یا ۴۸.',
    )
    mega_menu_image_gap = models.PositiveSmallIntegerField(
        default=8, verbose_name='فاصله‌ی تصویر تا عنوان (پیکسل)', help_text='بین ۰ تا ۳۲.',
    )
    mega_menu_show_banner = models.BooleanField(
        default=True, verbose_name='نمایش بنر دسته‌ها در مگامنو',
        help_text='بنر هر دسته‌ی اصلی در همان صفحه‌ی ویرایش دسته (بخش «بنر مگامنو») تعریف می‌شود؛ '
                  'دسته‌ای که بنر ندارد، ستون بنرش نمایش داده نمی‌شود.',
    )
    mega_menu_banner_width = models.PositiveSmallIntegerField(
        default=260, verbose_name='عرض ستون بنر مگامنو (پیکسل)', help_text='بین ۱۶۰ تا ۴۸۰.',
    )

    class Meta:
        verbose_name = 'تنظیمات سایت'
        verbose_name_plural = 'تنظیمات سایت'
        # هم‌ارز اعتبارسنجی clean() برای نوشتن‌های مستقیم (شل، اسکریپت) که full_clean نمی‌زنند
        constraints = [
            models.CheckConstraint(condition=Q(guest_price_level__gte=1, guest_price_level__lte=10),
                                   name='sitesettings_guest_price_level_1_to_10'),
            models.CheckConstraint(condition=Q(guest_price_rounding_step__in=[1, 100, 1000]),
                                   name='sitesettings_guest_rounding_step_allowed'),
            models.CheckConstraint(
                condition=~Q(guest_adjustment_type=ADJUST_PERCENT) | Q(
                    guest_adjustment_value__gte=GUEST_PERCENT_MIN, guest_adjustment_value__lte=GUEST_PERCENT_MAX),
                name='sitesettings_guest_percent_in_range',
            ),
            models.CheckConstraint(
                condition=Q(loyalty_threshold_bronze__lt=F('loyalty_threshold_silver')) &
                          Q(loyalty_threshold_silver__lt=F('loyalty_threshold_gold')) &
                          Q(loyalty_threshold_gold__lt=F('loyalty_threshold_diamond')),
                name='sitesettings_loyalty_thresholds_ascending',
            ),
            models.CheckConstraint(condition=Q(loyalty_amount_step__gte=1), name='sitesettings_loyalty_amount_step_gte_1'),
            models.CheckConstraint(condition=Q(loyalty_points_per_order__gte=1), name='sitesettings_loyalty_points_per_order_gte_1'),
            models.CheckConstraint(condition=Q(return_period_days__gte=1), name='sitesettings_return_period_days_gte_1'),
            models.CheckConstraint(
                condition=Q(return_period_unit__in=['working_days', 'calendar_days']),
                name='sitesettings_return_period_unit_allowed',
            ),
            models.CheckConstraint(condition=Q(return_attachment_max_image_mb__gte=1), name='sitesettings_return_attachment_max_image_mb_gte_1'),
            models.CheckConstraint(condition=Q(return_attachment_max_video_mb__gte=1), name='sitesettings_return_attachment_max_video_mb_gte_1'),
            models.CheckConstraint(
                condition=Q(site_content_max_width__gte=960, site_content_max_width__lte=2560),
                name='sitesettings_content_max_width_in_range',
            ),
            models.CheckConstraint(
                condition=Q(default_shop_columns__in=['grid-3', 'grid-4']),
                name='sitesettings_default_shop_columns_allowed',
            ),
            models.CheckConstraint(
                condition=Q(hero_slider_width_mode__in=['dynamic', 'full']),
                name='sitesettings_hero_slider_width_mode_allowed',
            ),
            models.CheckConstraint(
                condition=Q(dashboard_content_max_width__gte=960, dashboard_content_max_width__lte=2560),
                name='sitesettings_dashboard_content_max_width_in_range',
            ),
            models.CheckConstraint(
                condition=Q(admin_panel_max_width__isnull=True) |
                          Q(admin_panel_max_width__gte=960, admin_panel_max_width__lte=2560),
                name='sitesettings_admin_panel_max_width_in_range',
            ),
            models.CheckConstraint(
                condition=Q(mega_menu_width_mode='container') |
                          Q(mega_menu_width_mode='px', mega_menu_width_value__gte=600, mega_menu_width_value__lte=2560) |
                          Q(mega_menu_width_mode='percent', mega_menu_width_value__gte=50, mega_menu_width_value__lte=100),
                name='sitesettings_mega_menu_width_valid',
            ),
            models.CheckConstraint(
                condition=Q(mega_menu_max_height__gte=240, mega_menu_max_height__lte=800),
                name='sitesettings_mega_menu_max_height_in_range',
            ),
            models.CheckConstraint(condition=Q(mega_menu_columns__in=[3, 4, 5]), name='sitesettings_mega_menu_columns_allowed'),
            models.CheckConstraint(
                condition=Q(mega_menu_bg_opacity__gte=10, mega_menu_bg_opacity__lte=100),
                name='sitesettings_mega_menu_bg_opacity_in_range',
            ),
            models.CheckConstraint(
                condition=Q(mega_menu_blur_px__gte=0, mega_menu_blur_px__lte=40),
                name='sitesettings_mega_menu_blur_in_range',
            ),
            models.CheckConstraint(
                condition=Q(mega_menu_bg_image_mode__in=['cover', 'repeat']),
                name='sitesettings_mega_menu_bg_image_mode_allowed',
            ),
            models.CheckConstraint(
                condition=Q(mega_menu_image_position__in=['start', 'end']),
                name='sitesettings_mega_menu_image_position_allowed',
            ),
            models.CheckConstraint(
                condition=Q(mega_menu_image_size__gte=16, mega_menu_image_size__lte=64),
                name='sitesettings_mega_menu_image_size_in_range',
            ),
            models.CheckConstraint(
                condition=Q(mega_menu_image_gap__gte=0, mega_menu_image_gap__lte=32),
                name='sitesettings_mega_menu_image_gap_in_range',
            ),
            models.CheckConstraint(
                condition=Q(mega_menu_banner_width__gte=160, mega_menu_banner_width__lte=480),
                name='sitesettings_mega_menu_banner_width_in_range',
            ),
            models.CheckConstraint(
                condition=Q(map_type__in=['google', 'neshan', 'custom']), name='sitesettings_map_type_allowed',
            ),
            models.CheckConstraint(condition=Q(loyalty_redeem_toman_per_point__gte=1), name='sitesettings_loyalty_redeem_rate_gte_1'),
            models.CheckConstraint(condition=Q(loyalty_redeem_min_points__gte=1), name='sitesettings_loyalty_redeem_min_gte_1'),
            models.CheckConstraint(
                condition=Q(loyalty_redeem_min_points__lte=F('loyalty_redeem_max_points_per_transaction')),
                name='sitesettings_loyalty_redeem_min_lte_max_tx',
            ),
            models.CheckConstraint(
                condition=Q(loyalty_redeem_max_points_per_transaction__lte=F('loyalty_redeem_max_points_per_day')),
                name='sitesettings_loyalty_redeem_max_tx_lte_max_day',
            ),
        ]

    def __str__(self):
        return 'تنظیمات سایت'

    def clean(self):
        super().clean()
        errors = {}
        value = self.guest_adjustment_value
        if value is not None:
            if self.guest_adjustment_type == ADJUST_PERCENT:
                if not GUEST_PERCENT_MIN <= value <= GUEST_PERCENT_MAX:
                    errors['guest_adjustment_value'] = 'تعدیل درصدی باید بین ‎-۹۰ و ‎+۵۰۰ باشد.'
            elif value != value.to_integral_value():
                errors['guest_adjustment_value'] = 'مبلغ ثابت باید عدد صحیح (تومان) باشد؛ اعشار مجاز نیست.'
            if self.guest_pricing_mode == GUEST_CALCULATED_PRICE and value == Decimal('0') and 'guest_adjustment_value' not in errors:
                errors['guest_adjustment_value'] = ('در حالت «قیمت فرمولی» مقدار تعدیل نباید صفر باشد. برای نمایش بدون تعدیل '
                                                    'حالت «نمایش یکی از قیمت‌های ده‌گانه» را انتخاب کنید.')
        if not (self.guest_price_hidden_message or '').strip():
            errors['guest_price_hidden_message'] = 'متن راهنما نباید خالی باشد.'
        thresholds = (self.loyalty_threshold_bronze, self.loyalty_threshold_silver,
                      self.loyalty_threshold_gold, self.loyalty_threshold_diamond)
        if not (thresholds[0] < thresholds[1] < thresholds[2] < thresholds[3]):
            msg = 'آستانه‌های سطح باید صعودی باشند (برنزی < نقره‌ای < طلایی < الماسی).'
            errors['loyalty_threshold_bronze'] = msg
        if self.loyalty_amount_step < 1:
            errors['loyalty_amount_step'] = 'باید حداقل ۱ باشد.'
        if self.loyalty_points_per_order < 1:
            errors['loyalty_points_per_order'] = 'باید حداقل ۱ باشد.'
        if self.return_period_days < 1:
            errors['return_period_days'] = 'باید حداقل ۱ باشد.'
        if self.loyalty_redeem_min_points > self.loyalty_redeem_max_points_per_transaction:
            errors['loyalty_redeem_min_points'] = 'حداقل امتیاز نباید از حداکثر امتیاز هر تراکنش بیشتر باشد.'
        if self.loyalty_redeem_max_points_per_transaction > self.loyalty_redeem_max_points_per_day:
            errors['loyalty_redeem_max_points_per_transaction'] = 'حداکثر امتیاز هر تراکنش نباید از سقف روزانه بیشتر باشد.'
        if self.mega_menu_width_mode == self.MEGA_WIDTH_PX and not 600 <= self.mega_menu_width_value <= 2560:
            errors['mega_menu_width_value'] = 'در حالت «عرض ثابت» مقدار باید بین ۶۰۰ تا ۲۵۶۰ پیکسل باشد.'
        elif self.mega_menu_width_mode == self.MEGA_WIDTH_PERCENT and not 50 <= self.mega_menu_width_value <= 100:
            errors['mega_menu_width_value'] = 'در حالت «درصدی» مقدار باید بین ۵۰ تا ۱۰۰ باشد.'
        self._clean_store_identity(errors)
        for field, message in validate_chat_settings(self).items():
            errors.setdefault(field, message)
        if errors:
            raise ValidationError(errors)

    # فیلدهایی که ادمین ممکن است با ارقام فارسی تایپ کند؛ پیش از اعتبارسنجی و ذخیره به لاتین برمی‌گردند
    _DIGIT_FIELDS = (
        'store_national_id', 'store_registration_number', 'store_economic_code', 'store_postal_code',
        'store_phone_1', 'store_phone_2', 'store_mobile', 'store_admin_sms_recipient', 'store_admin_sms_recipient_2',
    )

    def _clean_store_identity(self, errors):
        """ اعتبارسنجی مشخصات حقوقی/تماس/نقشه‌ی فروشگاه (همه اختیاری‌اند؛ خالی بودن همیشه مجاز است) """
        for name in self._DIGIT_FIELDS:
            setattr(self, name, to_latin_digits((getattr(self, name) or '')).strip())

        def check(field, ok, message):
            if getattr(self, field) and not ok(getattr(self, field)) and field not in errors:
                errors[field] = message

        check('store_national_id', lambda v: re.fullmatch(r'\d{11}', v) or is_valid_iranian_national_code(v),
              'شناسه ملی باید ۱۱ رقم یا کد ملی معتبر ۱۰ رقمی باشد.')
        check('store_registration_number', lambda v: re.fullmatch(r'\d{1,20}', v), 'شماره ثبت فقط باید عدد باشد.')
        check('store_economic_code', lambda v: re.fullmatch(r'\d{12}|\d{14}', v), 'کد اقتصادی باید ۱۲ یا ۱۴ رقم باشد.')
        check('store_postal_code', lambda v: re.fullmatch(r'\d{10}', v), 'کد پستی باید ۱۰ رقم (بدون خط تیره) باشد.')
        for field in ('store_phone_1', 'store_phone_2'):
            check(field, lambda v: re.fullmatch(r'[0-9+\-\s()]{5,20}', v),
                  'شماره تلفن فقط می‌تواند عدد، فاصله، + و - داشته باشد (مثلاً 025-37700000).')
        for field in ('store_mobile', 'store_admin_sms_recipient', 'store_admin_sms_recipient_2'):
            check(field, lambda v: re.fullmatch(r'09\d{9}', v), 'شماره موبایل باید با فرمت 09123456789 باشد.')

        lat, lng = self.map_latitude, self.map_longitude
        if lat is not None and not -90 <= lat <= 90:
            errors['map_latitude'] = 'عرض جغرافیایی باید بین ‎-90 تا 90 باشد.'
        if lng is not None and not -180 <= lng <= 180:
            errors['map_longitude'] = 'طول جغرافیایی باید بین ‎-180 تا 180 باشد.'
        if (lat is None) != (lng is None):
            errors.setdefault('map_latitude' if lat is None else 'map_longitude', 'عرض و طول جغرافیایی باید با هم پر شوند.')
        if self.map_type == self.MAP_CUSTOM and not self.map_iframe_src:
            errors['map_iframe_code'] = ('کد iframe معتبر نیست: باید آدرس https از دامنه‌های مجاز '
                                         '(گوگل، OpenStreetMap، نشان، بلد) داشته باشد.')
        elif self.map_iframe_code.strip() and not self.map_iframe_src:
            errors['map_iframe_code'] = ('کد iframe معتبر نیست: باید آدرس https از دامنه‌های مجاز '
                                         '(گوگل، OpenStreetMap، نشان، بلد) داشته باشد.')

    def save(self, *args, **kwargs):
        self.pk = 1  # singleton: همیشه همین یک ردیف به‌روزرسانی می‌شود
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass  # جلوگیری از حذف تصادفی تنها ردیف تنظیمات سایت

    CACHE_KEY = 'storefront:site_settings'
    SVG_SOCIAL_ICONS = ('instagram', 'telegram')

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @classmethod
    def cached(cls):
        """
        همان load() ولی از کش ۱۵-دقیقه‌ای مشترک با context_processors._site_settings
        استفاده می‌کند. برای منطق سرور (هزینه ارسال در سبد/فاکتور/هلو) به‌جای load() این
        متد را صدا بزنید تا هر ریکوئست یک کوئری اضافه به این جدول نزند.
        """
        settings_obj = cache.get(cls.CACHE_KEY)
        if settings_obj is None:
            settings_obj = cls.load()
            cache.set(settings_obj.CACHE_KEY, settings_obj, 15 * 60)
        return settings_obj

    @property
    def mega_menu_inline_style(self):
        """
        مقدار صفت style پنل مگامنو (templates/base.html): متغیرهای CSS که بلوک .mega-* در app.css
        می‌خواند. خروجی فقط از عددهای صحیح و یک hex اعتبارسنجی‌شده ساخته می‌شود (نه متن آزاد)، حتی اگر
        مقدار با نوشتن مستقیم در DB (بدون full_clean) خراب شده باشد؛ تصویر پس‌زمینه جدا در تمپلیت
        می‌آید. فقط تم روشن: در حالت تیره قانون .dark .mega-panel همه‌ی این‌ها را نادیده می‌گیرد.
        """
        def clamp(value, low, high, default):
            try:
                return max(low, min(high, int(value)))
            except (TypeError, ValueError):
                return default

        hex_color = (self.mega_menu_bg_color or '').lstrip('#')
        try:
            if len(hex_color) != 6:
                raise ValueError
            red, green, blue = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            red = green = blue = 255
        opacity = clamp(self.mega_menu_bg_opacity, 10, 100, 100)
        blur = clamp(self.mega_menu_blur_px, 0, 40, 0)
        parts = [
            f'--mega-bg-rgb:{red} {green} {blue}',
            f'--mega-bg-opacity:{opacity / 100:g}',
            f'--mega-backdrop:{f"blur({blur}px)" if blur else "none"}',
            f'--mega-max-height:{clamp(self.mega_menu_max_height, 240, 800, 400)}px',
            f'--mega-cols:{clamp(self.mega_menu_columns, 3, 5, 4)}',
            f'--mega-img-size:{clamp(self.mega_menu_image_size, 16, 64, 32)}px',
            f'--mega-img-gap:{clamp(self.mega_menu_image_gap, 0, 32, 8)}px',
            f'--mega-img-dir:{"row-reverse" if self.mega_menu_image_position == self.MEGA_IMAGE_POSITION_END else "row"}',
            f'--mega-banner-width:{clamp(self.mega_menu_banner_width, 160, 480, 260)}px',
            f'--mega-bg-repeat:{"repeat" if self.mega_menu_bg_image_mode == self.MEGA_BG_REPEAT else "no-repeat"}',
            f'--mega-bg-size:{"auto" if self.mega_menu_bg_image_mode == self.MEGA_BG_REPEAT else "cover"}',
        ]
        value = clamp(self.mega_menu_width_value, 1, 2560, 1200)
        if self.mega_menu_width_mode == self.MEGA_WIDTH_PX:
            parts.append(f'width:min({max(value, 600)}px,100%)')
        elif self.mega_menu_width_mode == self.MEGA_WIDTH_PERCENT:
            parts.append(f'width:{max(50, min(value, 100))}%')
        return ';'.join(parts)

    # ---- مشخصات فروشگاه برای قالب‌ها ----
    @property
    def store_contact_phones(self):
        """ تلفن‌ها/موبایل پرشده، به‌صورت [{'display', 'href'}] برای لینک tel: در فوتر و صفحه‌ی تماس """
        result = []
        for value in (self.store_phone_1, self.store_phone_2, self.store_mobile):
            if value:
                result.append({'display': value, 'href': 'tel:' + re.sub(r'[^0-9+]', '', value)})
        return result

    @property
    def store_admin_sms_recipients(self):
        """ شماره‌های مدیر برای پیامک‌های سیستمی، پرشده و بدون تکرار، به ترتیب (۱ سپس ۲) """
        result = []
        for number in (self.store_admin_sms_recipient, self.store_admin_sms_recipient_2):
            if number and number not in result:
                result.append(number)
        return result

    @property
    def store_working_hours_rows(self):
        """ ساعات کاری به ردیف‌های (عنوان، مقدار): خط «روز: ساعت» دوستونه، خط بدون «:» تمام‌عرض (عنوان خالی) """
        rows = []
        for line in (self.store_working_hours or '').splitlines():
            line = line.strip()
            if not line:
                continue
            label, sep, value = line.partition(':')
            rows.append((label.strip(), value.strip()) if sep and value.strip() else ('', line))
        return rows

    @property
    def map_iframe_src(self):
        """ آدرس src معتبر از کد iframe دلخواه (فقط https از دامنه‌های مجاز)؛ وگرنه '' - بقیه‌ی کد هرگز رندر نمی‌شود """
        match = re.search(r'<iframe\b[^>]*?\ssrc\s*=\s*["\']([^"\']+)["\']', self.map_iframe_code or '', re.IGNORECASE)
        if not match:
            return ''
        src = match.group(1).strip()
        parsed = urlparse(src)
        if parsed.scheme != 'https' or parsed.hostname not in self.MAP_IFRAME_ALLOWED_HOSTS:
            return ''
        if parsed.hostname.endswith('google.com') and not parsed.path.startswith('/maps'):
            return ''
        return src

    @property
    def map_embed_src(self):
        """ آدرس iframe نقشه‌ی صفحه‌ی تماس، یا '' اگر چیزی برای نمایش نیست """
        if self.map_type == self.MAP_CUSTOM:
            return self.map_iframe_src
        if self.map_latitude is None or self.map_longitude is None:
            return ''
        lat, lng = float(self.map_latitude), float(self.map_longitude)
        if self.map_type == self.MAP_NESHAN:
            delta = 0.004
            return (f'https://www.openstreetmap.org/export/embed.html?bbox={lng - delta:.6f},{lat - delta:.6f},'
                    f'{lng + delta:.6f},{lat + delta:.6f}&layer=mapnik&marker={lat:.6f},{lng:.6f}')
        return f'https://maps.google.com/maps?q={lat:.6f},{lng:.6f}&z=16&output=embed'

    @property
    def map_directions_url(self):
        """ لینک مسیریابی گوگل‌مپ به مختصات فروشگاه (اگر مختصات پر باشد) """
        if self.map_latitude is None or self.map_longitude is None:
            return ''
        return f'https://www.google.com/maps/dir/?api=1&destination={float(self.map_latitude):.6f},{float(self.map_longitude):.6f}'

    @property
    def map_geo_uri(self):
        """
        آدرس geo: استاندارد اندروید برای دکمه‌ی «مسیریابی»: مرورگر موبایل با آن فهرست همه‌ی برنامه‌های نقشه/مسیریاب
        نصب‌شده روی گوشی (گوگل‌مپ، ویز، نشان، بلد، ...) را به کاربر نشان می‌دهد تا خودش انتخاب کند.
        """
        if self.map_latitude is None or self.map_longitude is None:
            return ''
        lat, lng = f'{float(self.map_latitude):.6f}', f'{float(self.map_longitude):.6f}'
        return f'geo:{lat},{lng}?q={lat},{lng}({quote(self.store_name or "")})'

    @property
    def map_route_links(self):
        """
        لینک‌های مسیریابی برای دستگاه‌هایی که geo: فهرست برنامه‌ها را نشان نمی‌دهند (آیفون، دسکتاپ):
        [{'label', 'url'}]. لینک نشان/بلد همان است که ادمین در map_neshan_url گذاشته (قالب لینک آن‌ها
        بدون کلید/مستندات رسمی قابل ساخت نیست).
        """
        if self.map_latitude is None or self.map_longitude is None:
            return []
        lat, lng = f'{float(self.map_latitude):.6f}', f'{float(self.map_longitude):.6f}'
        links = [
            {'label': 'گوگل‌مپ', 'url': f'https://www.google.com/maps/dir/?api=1&destination={lat},{lng}'},
            {'label': 'ویز (Waze)', 'url': f'https://waze.com/ul?ll={lat},{lng}&navigate=yes'},
            {'label': 'اپل مپ', 'url': f'https://maps.apple.com/?daddr={lat},{lng}&dirflg=d'},
        ]
        if self.map_neshan_url:
            links.append({'label': 'نشان / بلد', 'url': self.map_neshan_url})
        return links

    # ---- محتوای صفحه‌ی «درباره ما» ----
    @property
    def about_story_paragraphs(self):
        return [p.strip() for p in re.split(r'\n\s*\n', (self.about_story_text or '').replace('\r\n', '\n')) if p.strip()]

    @property
    def about_value_cards(self):
        """ کارت‌های ماموریت/ارزش‌ها که عنوان یا متن دارند: [{'title','text','icon_path'}] """
        cards = []
        for index in (1, 2, 3):
            title = getattr(self, f'about_value{index}_title')
            text = getattr(self, f'about_value{index}_text')
            if title or text:
                icon = self.ABOUT_ICONS.get(getattr(self, f'about_value{index}_icon'))
                cards.append({'title': title, 'text': text, 'icon_path': icon[1] if icon else ''})
        return cards

    @property
    def about_stats(self):
        """
        آمارهایی که هم عدد دارند هم عنوان: [{'value','label','target','digits','display','prefix','suffix','fa_digits'}].
        value همان متن ادمین است (بدون JS همین نشان داده می‌شود)؛ اگر متن «پیشوند + عدد صحیح + پسوند» بود
        (مثل «۱۰+» یا «۹۸٪»)، target/prefix/suffix برای شمارنده‌ی انیمیشنی صفحه‌ی «درباره ما» پر می‌شود و
        fa_digits می‌گوید عدد با ارقام فارسی نوشته شده تا شمارنده هم همان رقم‌ها را نشان بدهد. وگرنه target=None.
        """
        stats = []
        for index in (1, 2, 3, 4):
            value = getattr(self, f'about_stat{index}_value')
            label = getattr(self, f'about_stat{index}_label')
            if not (value and label):
                continue
            item = {'value': value, 'label': label, 'target': None, 'digits': '', 'display': value, 'prefix': '',
                    'suffix': '', 'fa_digits': False}
            match = re.fullmatch(r'(\D*?)([0-9۰-۹٠-٩][0-9۰-۹٠-٩٬,،]*)(\D*)', value.strip())
            if match:
                digits = match.group(2)
                # جداکننده‌ی هزارگان (٬ یا , یا ،) فقط ظاهر است؛ عدد خام بدون آن‌ها خوانده می‌شود
                target = int(re.sub(r'[\u066c,\u060c]', '', to_latin_digits(digits)))
                fa_digits = not digits.isascii()
                item.update(
                    target=target, digits=digits, display=format_grouped_number(target, fa_digits),
                    prefix=match.group(1), suffix=match.group(3), fa_digits=fa_digits,
                )
            stats.append(item)
        return stats

    @property
    def social_links(self):
        """ فقط لینک‌های شبکه اجتماعی‌ای که ادمین واقعاً پر کرده، برای حلقه‌زدن در فوتر.
        icon دقیقاً هم‌نام فایل‌های static/theme/assets/images/social/ است (که املای
        eitta/sorush را دارند، نه eitaa/soroush؛ اسم فیلد مدل با اسم فایل یکی نیست). """
        fields = (
            (self.rubika_url, 'rubika', 'روبیکا'),
            (self.aparat_url, 'aparat', 'آپارات'),
            (self.bale_url, 'bale', 'بله'),
            (self.eitaa_url, 'eitta', 'ایتا'),
            (self.igap_url, 'igap', 'آی‌گپ'),
            (self.soroush_url, 'sorush', 'سروش'),
            (self.instagram_url, 'instagram', 'اینستاگرام'),
            (self.telegram_url, 'telegram', 'تلگرام'),
        )
        # file = نام کامل فایل در social/ (آیکون‌های اینستاگرام/تلگرام svg‌اند، بقیه png)
        return [
            {'icon': icon, 'file': f'{icon}.svg' if icon in self.SVG_SOCIAL_ICONS else f'{icon}.png',
             'url': url, 'label': label}
            for url, icon, label in fields if url
        ]

    @property
    def app_download_links(self):
        """ همان الگوی social_links؛ فقط اپ‌هایی که ادمین لینک‌شان را پر کرده برمی‌گردند.
        icon هم‌نام فایل‌های static/theme/assets/images/application/ است. """
        fields = (
            (self.app_google_play_url, 'google-play-app.svg', 'دانلود از گوگل‌پلی'),
            (self.app_sibapp_url, 'app-sibapp.svg', 'دانلود از سیب‌اپ'),
            (self.app_bazaar_url, 'bazar-app.svg', 'دانلود از کافه‌بازار'),
            (self.app_myket_url, 'myket-app.png', 'دانلود از مایکت'),
        )
        return [{'icon': icon, 'url': url, 'label': label} for url, icon, label in fields if url]


class NewsletterSubscriber(models.Model):
    """ فرم «عضویت در خبرنامه» فوتر؛ فقط شماره موبایل - مثل مرجع (نه ایمیل) """
    phone_number = models.CharField(max_length=15, unique=True, verbose_name='شماره موبایل')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'عضو خبرنامه'
        verbose_name_plural = 'اعضای خبرنامه'
        ordering = ('-created_at',)

    def __str__(self):
        return self.phone_number


class HomeBanner(models.Model):
    """
    بنرهای تبلیغاتی صفحه اصلی؛ برخلاف CategoryBanner (لیست آزاد)، اینجا دقیقاً ۴ جایگاه
    ثابت داریم (SPECIAL_OFFER زیر دسته‌بندی، ROW_LEFT/ROW_RIGHT کنار هم، BRANDS زیر
    محبوب‌ترین برندها) - نگاه کنید HomeBannerAdmin که افزودن/حذف ردیف را می‌بندد.
    """
    SPECIAL_OFFER = 'special_offer'
    ROW_LEFT = 'row_left'
    ROW_RIGHT = 'row_right'
    BRANDS = 'brands'
    SLOT_CHOICES = (
        (SPECIAL_OFFER, 'تخفیف ویژه (زیر دسته‌بندی)'),
        (ROW_LEFT, 'بنر ردیف دوتایی - سمت چپ'),
        (ROW_RIGHT, 'بنر ردیف دوتایی - سمت راست'),
        (BRANDS, 'بنر بزرگ (زیر محبوب‌ترین برندها)'),
    )

    NONE = 'none'
    URL = 'url'
    PRODUCT = 'product'
    CATEGORY = 'category'
    LINK_TYPE_CHOICES = (
        (NONE, 'بدون لینک'),
        (URL, 'لینک دلخواه'),
        (PRODUCT, 'یک محصول'),
        (CATEGORY, 'یک دسته‌بندی'),
    )

    slot = models.CharField(max_length=20, choices=SLOT_CHOICES, unique=True, verbose_name='جایگاه')
    image = models.ImageField(upload_to='banners/home/', verbose_name='تصویر بنر')
    alt_text = models.CharField(max_length=200, blank=True, verbose_name='متن جایگزین تصویر (alt)')

    link_type = models.CharField(max_length=10, choices=LINK_TYPE_CHOICES, default=NONE, verbose_name='نوع لینک')
    link_url = models.CharField(max_length=500, blank=True, verbose_name='لینک دلخواه')
    link_product = models.ForeignKey(
        'Product', null=True, blank=True, on_delete=models.SET_NULL, related_name='+', verbose_name='محصول مقصد',
    )
    link_category = models.ForeignKey(
        Category, null=True, blank=True, on_delete=models.SET_NULL, related_name='+', verbose_name='دسته‌بندی مقصد',
    )

    is_active = models.BooleanField(default=True, verbose_name='فعال (نمایش داده شود)')

    class Meta:
        verbose_name = 'بنر صفحه اصلی'
        verbose_name_plural = 'بنرهای صفحه اصلی'
        ordering = ('slot',)

    def __str__(self):
        return self.get_slot_display()

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.link_type == self.URL and not self.link_url:
            raise ValidationError({'link_url': 'برای نوع لینک «لینک دلخواه»، این فیلد الزامی است.'})
        if self.link_type == self.PRODUCT and not self.link_product_id:
            raise ValidationError({'link_product': 'برای نوع لینک «یک محصول»، این فیلد الزامی است.'})
        if self.link_type == self.CATEGORY and not self.link_category_id:
            raise ValidationError({'link_category': 'برای نوع لینک «یک دسته‌بندی»، این فیلد الزامی است.'})

    @property
    def target_url(self):
        if self.link_type == self.PRODUCT and self.link_product_id:
            return reverse('products:product_detail', args=[self.link_product.slug])
        if self.link_type == self.CATEGORY and self.link_category_id:
            return reverse('products:category_detail', args=[self.link_category.slug])
        if self.link_type == self.URL:
            return self.link_url or ''
        return ''


class HeroSlide(models.Model):
    """
    اسلایدهای اسلایدر اصلی صفحه‌ی نخست (بالای صفحه). برخلاف HomeBanner (دقیقاً ۴ جایگاه ثابت)،
    اینجا یک لیست آزاد و قابل‌ترتیب است - هر تعداد اسلاید که ادمین بخواهد، با order قابل جابه‌جایی.
    """
    NONE = 'none'
    URL = 'url'
    PRODUCT = 'product'
    CATEGORY = 'category'
    LINK_TYPE_CHOICES = (
        (NONE, 'بدون لینک'),
        (URL, 'لینک دلخواه'),
        (PRODUCT, 'یک محصول'),
        (CATEGORY, 'یک دسته‌بندی'),
    )

    image = models.ImageField(upload_to='home/slider/', verbose_name='تصویر اسلاید')
    alt_text = models.CharField(max_length=200, blank=True, verbose_name='متن جایگزین تصویر (alt)')
    order = models.PositiveIntegerField(default=0, verbose_name='ترتیب نمایش')
    is_active = models.BooleanField(default=True, verbose_name='فعال (نمایش داده شود)')

    link_type = models.CharField(max_length=10, choices=LINK_TYPE_CHOICES, default=NONE, verbose_name='نوع لینک')
    link_url = models.CharField(max_length=500, blank=True, verbose_name='لینک دلخواه')
    link_product = models.ForeignKey(
        'Product', null=True, blank=True, on_delete=models.SET_NULL, related_name='+', verbose_name='محصول مقصد',
    )
    link_category = models.ForeignKey(
        Category, null=True, blank=True, on_delete=models.SET_NULL, related_name='+', verbose_name='دسته‌بندی مقصد',
    )

    class Meta:
        verbose_name = 'اسلاید صفحه اصلی'
        verbose_name_plural = 'اسلایدهای صفحه اصلی'
        ordering = ('order', 'id')

    def __str__(self):
        return f"اسلاید #{self.pk} ({self.get_link_type_display()})"

    def clean(self):
        if self.link_type == self.URL and not self.link_url:
            raise ValidationError({'link_url': 'برای نوع لینک «لینک دلخواه»، این فیلد الزامی است.'})
        if self.link_type == self.PRODUCT and not self.link_product_id:
            raise ValidationError({'link_product': 'برای نوع لینک «یک محصول»، این فیلد الزامی است.'})
        if self.link_type == self.CATEGORY and not self.link_category_id:
            raise ValidationError({'link_category': 'برای نوع لینک «یک دسته‌بندی»، این فیلد الزامی است.'})

    @property
    def target_url(self):
        if self.link_type == self.PRODUCT and self.link_product_id:
            return reverse('products:product_detail', args=[self.link_product.slug])
        if self.link_type == self.CATEGORY and self.link_category_id:
            return reverse('products:category_detail', args=[self.link_category.slug])
        if self.link_type == self.URL:
            return self.link_url or ''
        return ''


# ==========================================
# ۵. اطلاع موجودی («وقتی موجود شد خبرم کن»)
# ==========================================
class StockAlert(models.Model):
    """
    درخواست یک کاربر برای اطلاع‌رسانی وقتی یک محصولِ ناموجود دوباره موجود شود.

    هر (محصول، کاربر) فقط یک ردیف دارد و بین چرخه‌های ناموجود/موجود دوباره استفاده می‌شود
    (به‌جای انباشتن تاریخچه‌ی بی‌فایده): وقتی موجودی سینک هلو از صفر بیشتر شد، ردیف‌های
    pending همان محصول notified می‌شوند (holoo/tasks.py -> products.signals.product_back_in_stock)؛
    اگر کاربر بعداً دوباره روی همان محصولِ دوباره‌ناموجود‌شده کلیک کند، همین ردیف به pending
    برمی‌گردد.
    """
    CHANNEL_SMS = 'sms'
    CHANNEL_EMAIL = 'email'
    CHANNEL_BOTH = 'both'  # کاربر هر دو روش (پیامک و ایمیل) را انتخاب کرده
    CHANNEL_CHOICES = (
        (CHANNEL_SMS, 'پیامک'),
        (CHANNEL_EMAIL, 'ایمیل'),
        (CHANNEL_BOTH, 'پیامک و ایمیل'),
    )

    STATUS_PENDING = 'pending'
    STATUS_NOTIFIED = 'notified'
    STATUS_CHOICES = (
        (STATUS_PENDING, 'در انتظار موجود شدن'),
        (STATUS_NOTIFIED, 'اطلاع داده شد'),
    )

    product = models.ForeignKey(Product, related_name='stock_alerts', on_delete=models.CASCADE, verbose_name='محصول')
    user = models.ForeignKey(CustomUser, related_name='stock_alerts', on_delete=models.CASCADE, verbose_name='کاربر')
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES, verbose_name='کانال اطلاع‌رسانی')
    # ایمیل مخصوص همین درخواست؛ فقط وقتی از ایمیل پروفایل کاربر متفاوت باشد پر می‌شود (کاربری
    # که در پروفایلش از قبل ایمیل دارد می‌تواند اینجا ایمیل دیگری برای همین اطلاع‌رسانی بدهد
    # بدون اینکه ایمیل پروفایلش عوض شود). خالی یعنی از همان ایمیل پروفایل استفاده شود.
    email = models.EmailField(blank=True, verbose_name='ایمیل اختصاصی این درخواست')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True, verbose_name='وضعیت')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='تاریخ ثبت درخواست')
    notified_at = models.DateTimeField(null=True, blank=True, verbose_name='تاریخ اطلاع‌رسانی')

    class Meta:
        verbose_name = 'درخواست اطلاع موجودی'
        verbose_name_plural = 'درخواست‌های اطلاع موجودی'
        unique_together = ('product', 'user')

    def __str__(self):
        return f"{self.user.phone_number} <- {self.product.name} ({self.get_status_display()})"


class ContactMessage(models.Model):
    """
    پیام‌های فرم «تماس با ما». هر پیام کامل ثبت می‌شود (حتی اگر پیامک اعلان به مدیر نرسد) و مدیر از پنل
    ادمین وضعیتش را عوض و پاسخ/یادداشت ثبت می‌کند. فرم: products/contact.py؛ اعلان پیامکی با سیگنال دامنه‌ی
    products.signals.contact_message_received (شنونده در notifications/receivers.py).
    """
    STATUS_NEW = 'new'
    STATUS_IN_PROGRESS = 'in_progress'
    STATUS_ANSWERED = 'answered'
    STATUS_CLOSED = 'closed'
    STATUS_CHOICES = (
        (STATUS_NEW, 'جدید'),
        (STATUS_IN_PROGRESS, 'در حال بررسی'),
        (STATUS_ANSWERED, 'پاسخ داده شد'),
        (STATUS_CLOSED, 'بسته شد'),
    )

    name = models.CharField(max_length=100, verbose_name='نام فرستنده')
    phone = models.CharField(max_length=11, blank=True, verbose_name='شماره موبایل')
    email = models.EmailField(blank=True, verbose_name='ایمیل')
    subject = models.CharField(max_length=150, verbose_name='موضوع')
    message = models.TextField(max_length=2000, verbose_name='متن پیام')
    user = models.ForeignKey(
        CustomUser, null=True, blank=True, on_delete=models.SET_NULL, related_name='contact_messages',
        verbose_name='کاربر (اگر وارد شده بود)',
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True, verbose_name='IP فرستنده')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='تاریخ ثبت')

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_NEW, db_index=True, verbose_name='وضعیت بررسی')
    admin_reply = models.TextField(blank=True, verbose_name='پاسخ / یادداشت مدیر')
    replied_at = models.DateTimeField(null=True, blank=True, verbose_name='تاریخ آخرین پاسخ')

    class Meta:
        verbose_name = 'پیام تماس با ما'
        verbose_name_plural = 'پیام‌های تماس با ما'
        ordering = ('-created_at',)
        indexes = [models.Index(fields=['status', 'created_at'])]
        constraints = [
            models.CheckConstraint(
                condition=~Q(phone='') | ~Q(email=''), name='contactmessage_phone_or_email_required',
            ),
            models.CheckConstraint(
                condition=Q(status__in=['new', 'in_progress', 'answered', 'closed']),
                name='contactmessage_status_allowed',
            ),
        ]

    def __str__(self):
        return f'{self.name} - {self.subject}'


class StockReservation(models.Model):
    """
    دفتر رزرو موجودی یک سفارش برای یک کالا (products/stock.py). سفارش فقط با order_id (عدد) شناخته می‌شود تا لایه‌ی
    products به orders وابسته نشود (هم‌الگوی رزرو کد تخفیف). ستون Product.reserved_quantity جمع ردیف‌های فعال
    (held/invoiced) این جدول است و هر لحظه از روی آن بازمحاسبه‌پذیر است.
    """
    HELD = 'held'
    INVOICED = 'invoiced'
    EXPIRED = 'expired'
    RELEASED = 'released'
    STATE_CHOICES = (
        (HELD, 'رزرو شده'),
        (INVOICED, 'فاکتور در هلو ثبت شد (تا سینک بعدی نگه‌داشته می‌شود)'),
        (EXPIRED, 'منقضی شد'),
        (RELEASED, 'آزاد شد'),
    )

    order_id = models.PositiveBigIntegerField(db_index=True, verbose_name='شناسه سفارش')
    product = models.ForeignKey(Product, related_name='reservations', on_delete=models.CASCADE, verbose_name='محصول')
    quantity = models.PositiveIntegerField(verbose_name='تعداد')
    state = models.CharField(max_length=10, choices=STATE_CHOICES, default=HELD, db_index=True, verbose_name='وضعیت')
    expires_at = models.DateTimeField(null=True, blank=True, verbose_name='انقضای رزرو',
                                      help_text='خالی = تا تصمیم مدیر (سفارش پرداخت‌شده/چکی) یا آزادسازی پس از سینک می‌ماند.')
    invoiced_at = models.DateTimeField(null=True, blank=True, verbose_name='زمان ثبت فاکتور در هلو')
    released_at = models.DateTimeField(null=True, blank=True, verbose_name='زمان آزادسازی')
    release_reason = models.CharField(max_length=60, blank=True, default='', verbose_name='دلیل آزادسازی')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='ایجاد')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='آخرین تغییر')

    class Meta:
        verbose_name = 'رزرو موجودی'
        verbose_name_plural = 'رزروهای موجودی'
        ordering = ('-id',)
        constraints = [
            models.UniqueConstraint(fields=['order_id', 'product'], name='stockreservation_one_row_per_order_product'),
            models.CheckConstraint(condition=models.Q(quantity__gt=0), name='stockreservation_quantity_gt_0'),
        ]

    def __str__(self):
        return f'رزرو سفارش #{self.order_id} - {self.product_id} × {self.quantity} ({self.state})'
