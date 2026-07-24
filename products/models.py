from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django_ckeditor_5.fields import CKEditor5Field

from services.text import normalize_persian

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
        این متد جادویی، کاربر را می‌گیرد و قیمت مناسب او را برمی‌گرداند.
        اگر کاربر لاگین نبود، همان قیمت 1 (عادی) را می‌دهد.
        اگر قیمت سطح کاربر صفر بود (در هلو پر نشده بود)، باز هم قیمت 1 را می‌دهد.
        """
        if user and user.is_authenticated:
            level = getattr(user, 'price_level', 1)
            if level == 1:
                return self.price
            
            # استخراج قیمت از فیلد مورد نظر (مثلا price3)
            specific_price = getattr(self, f'price{level}', 0)
            return specific_price if specific_price > 0 else self.price

        return self.price

    @property
    def active_discount(self):
        """ بهترین (بیشترین درصد) تخفیف فعال این لحظه، یا None. برای نمایش کارت/برچسب. """
        now = timezone.now()
        return self.discounts.filter(is_active=True, starts_at__lte=now, ends_at__gte=now).order_by('-percent').first()

    def get_discounted_price(self, user):
        """ قیمت نهایی با احتساب سطح کاربر (get_user_price) و سپس تخفیف درصدی روی همان مبلغ """
        base_price = self.get_user_price(user)
        discount = self.active_discount
        return base_price - (base_price * discount.percent / 100) if discount else base_price

    def save(self, *args, **kwargs):
        self.name_normalized = normalize_persian(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Discount(models.Model):
    """ لایه‌ی محاسباتی تخفیف («شگفت‌انگیز»)؛ هرگز Product.price/price2..10 (سینک‌شده از هلو) را تغییر نمی‌دهد """
    product = models.ForeignKey(Product, related_name='discounts', on_delete=models.CASCADE, verbose_name='محصول')
    percent = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(99)], verbose_name='درصد تخفیف')
    starts_at = models.DateTimeField(verbose_name='شروع')
    ends_at = models.DateTimeField(verbose_name='پایان')
    is_active = models.BooleanField(default=True, verbose_name='فعال')

    class Meta:
        verbose_name = 'تخفیف'
        verbose_name_plural = 'تخفیف‌ها'
        ordering = ('-starts_at',)

    def __str__(self):
        return f"{self.product.name} — {self.percent}٪"

    @property
    def is_currently_active(self):
        now = timezone.now()
        return self.is_active and self.starts_at <= now <= self.ends_at


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