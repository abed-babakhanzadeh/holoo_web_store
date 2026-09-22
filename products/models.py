from django.core.cache import cache
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator
from django.db import models
from django.db.models import Q
from accounts.models import CustomUser
from django.urls import reverse
from django.utils import timezone
from django_ckeditor_5.fields import CKEditor5Field

from services.text import normalize_persian

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

    class Meta:
        verbose_name = 'دسته‌بندی'
        verbose_name_plural = 'دسته‌بندی‌ها'

    def __str__(self):
        return f"{self.parent.name} -> {self.name}" if self.parent else self.name

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

    def save(self, *args, **kwargs):
        self.name_normalized = normalize_persian(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


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
class SiteSettings(models.Model):
    """ تک‌ردیفی (singleton)؛ تنظیمات فوتر که در همه‌ی صفحات از طریق context processor در دسترس است """
    phone = models.CharField(max_length=32, blank=True, verbose_name='شماره تماس')
    email = models.EmailField(blank=True, verbose_name='آدرس ایمیل')
    working_hours_text = models.CharField(
        max_length=200, blank=True,
        default='هفت روز هفته، ۲۴ ساعت شبانه‌روز پاسخگوی شما هستیم.',
        verbose_name='متن ساعت پاسخگویی',
    )

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

    rubika_url = models.URLField(blank=True, verbose_name='لینک روبیکا')
    aparat_url = models.URLField(blank=True, verbose_name='لینک آپارات')
    bale_url = models.URLField(blank=True, verbose_name='لینک بله')
    eitaa_url = models.URLField(blank=True, verbose_name='لینک ایتا')
    igap_url = models.URLField(blank=True, verbose_name='لینک آی‌گپ')
    soroush_url = models.URLField(blank=True, verbose_name='لینک سروش')

    # هزینه‌ی ارسال دیگر عدد ثابت نیست: کرایه‌ی پیک از تعرفه‌ی ناحیه‌ی آدرس (locations.DeliveryZone) می‌آید و
    # پست، پس‌کرایه است (orders/shipping.py). فقط کد ردیفِ کرایه‌ی پیک در فاکتور هلو اینجا می‌ماند.
    shipping_erp_code = models.CharField(
        max_length=100, default='999999', verbose_name='ErpCode ردیف کرایه‌ی پیک در هلو',
        help_text='کد کالای هزینه ارسال که هنگام ثبت فاکتور در هلو برای سفارش‌های ارسال با پیک (با کرایه‌ی بیشتر از صفر) '
                  'به‌عنوان یک ردیف اضافه می‌شود.',
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

    show_stories = models.BooleanField(default=True, verbose_name='نمایش بخش استوری در صفحه اصلی')

    show_newsletter = models.BooleanField(default=True, verbose_name='نمایش عضویت در خبرنامه (فوتر)')
    show_app_download = models.BooleanField(default=True, verbose_name='نمایش دانلود اپلیکیشن (فوتر)')
    app_google_play_url = models.URLField(blank=True, verbose_name='لینک گوگل‌پلی')
    app_sibapp_url = models.URLField(blank=True, verbose_name='لینک سیب‌اپ')
    app_bazaar_url = models.URLField(blank=True, verbose_name='لینک کافه‌بازار')
    app_myket_url = models.URLField(blank=True, verbose_name='لینک مایکت')
    app_direct_download_url = models.URLField(blank=True, verbose_name='لینک دانلود مستقیم')

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
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.pk = 1  # singleton: همیشه همین یک ردیف به‌روزرسانی می‌شود
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass  # جلوگیری از حذف تصادفی تنها ردیف تنظیمات سایت

    CACHE_KEY = 'storefront:site_settings'

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
        )
        return [{'icon': icon, 'url': url, 'label': label} for url, icon, label in fields if url]

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