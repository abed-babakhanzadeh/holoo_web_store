from django.db import models
from django.urls import reverse
from django_ckeditor_5.fields import CKEditor5Field

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

    class Meta:
        verbose_name = 'دسته‌بندی'
        verbose_name_plural = 'دسته‌بندی‌ها'

    def __str__(self):
        return f"{self.parent.name} -> {self.name}" if self.parent else self.name

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
# 2. هسته اصلی محصول (متصل به هلو)
# ==========================================
class Product(models.Model):
    category = models.ForeignKey(Category, related_name='products', on_delete=models.SET_NULL, null=True, verbose_name='دسته‌بندی فرعی')
    brand = models.ForeignKey(Brand, related_name='products', on_delete=models.SET_NULL, null=True, blank=True, verbose_name='برند')
    name = models.CharField(max_length=255, verbose_name='نام کالا')
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
    warranty = models.CharField(max_length=100, blank=True, verbose_name='گارانتی')

    is_active = models.BooleanField(default=True, verbose_name='نمایش در سایت')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

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