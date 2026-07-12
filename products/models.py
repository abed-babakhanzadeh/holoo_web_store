from django.db import models
from django.urls import reverse

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


# ==========================================
# 2. هسته اصلی محصول (متصل به هلو)
# ==========================================
class Product(models.Model):
    category = models.ForeignKey(Category, related_name='products', on_delete=models.SET_NULL, null=True, verbose_name='دسته‌بندی فرعی')
    name = models.CharField(max_length=255, verbose_name='نام کالا')
    slug = models.SlugField(max_length=255, unique=True, allow_unicode=True, verbose_name='اسلاگ')
    
    # -- فیلدهای یکپارچه با هلو (مالی و انبار) --
    erp_code = models.CharField(max_length=100, unique=True, db_index=True, verbose_name='ErpCode هلو')
    product_code = models.CharField(max_length=50, blank=True, null=True, verbose_name='کد کالا') # مثلا 00202010 در عکس شما
    
    # قیمت‌ها: قیمت اصلی را SellPrice میگیریم. 
    # اگر در آینده نیاز به قیمت‌های عمده‌فروشی بود (SellPrice2 تا 10)، فیلدهای جدید اضافه می‌کنیم.
    price = models.DecimalField(max_digits=12, decimal_places=0, default=0, verbose_name='قیمت فروش (تومان)')
    stock = models.FloatField(default=0, verbose_name='موجودی')
    
    # -- فیلدهای اختصاصی وب‌سایت (نمایشی) --
    description = models.TextField(blank=True, null=True, verbose_name='توضیحات معرفی')
    main_image = models.ImageField(upload_to='products/main/', blank=True, null=True, verbose_name='تصویر اصلی سایت')
    
    is_active = models.BooleanField(default=True, verbose_name='نمایش در سایت')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'محصول'
        verbose_name_plural = 'محصولات'
        indexes = [models.Index(fields=['erp_code'])]

    def __str__(self):
        return self.name


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