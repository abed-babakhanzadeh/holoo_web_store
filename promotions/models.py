"""
مدل‌های تخفیف خودکار (Promotion) و سیاست سراسری تخفیف (DiscountPolicy).

تخفیف‌ها یک «لایه‌ی محاسباتی» روی قیمت‌اند و هرگز Product.price/price2..10 (سینک‌شده از هلو) را تغییر نمی‌دهند.
ترتیب محاسبه‌ی کامل قیمت در سرِ products/pricing.py مستند است.
"""

import re

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from accounts.models import CustomUser
from products.models import Brand, Category, Product

# سطح‌های وفاداری از خود CustomUser.LOYALTY_LEVELS؛ 0 = بدون محدودیت
LOYALTY_CHOICES = [(0, 'همه (بدون محدودیت سطح)')] + [
    (index, f'سطح «{label}» و بالاتر') for index, (_threshold, label) in enumerate(CustomUser.LOYALTY_LEVELS) if index >= 1
]

_PRICE_LEVELS_RE = re.compile(r'^\d{1,2}(,\d{1,2})*$')


class DiscountPolicy(models.Model):
    """
    سیاست سراسری *تخفیف‌های خودکار* (تک‌ردیفی). ادمین بدون تغییر کد تعیین می‌کند تخفیف‌های خودکار (Promotion) برای
    چه کسانی، چگونه و تا چه سقفی اعمال شوند. مقدار فعلیِ آن همراه با تخفیف‌های فعال در یک شاخص کش‌شده
    (promotions/index.py) می‌آید.

    محدوده: همه‌ی فیلدهای این مدل (از جمله apply_to_vip، apply_for_cash/check، ترکیب و سقف) *فقط* در
    promotions/resolver.py و فقط برای Promotion خوانده می‌شوند. کدهای تخفیف (کوپن، مرحله‌ی ۳) هیچ‌کدام از این
    فلگ‌ها را نمی‌خوانند و قواعد مخاطبِ خودشان را دارند (مثلاً «برای کاربران ویژه مجاز باشد» روی خودِ کوپن).
    """
    STACK_BEST = 'best'
    STACK_SUM = 'stack'
    STACKING_CHOICES = (
        (STACK_BEST, 'بهترین تخفیف (فقط تخفیفی که بیشترین صرفه را برای خریدار دارد)'),
        (STACK_SUM, 'جمع‌شونده (همه‌ی تخفیف‌های مشمول به‌ترتیب اولویت روی هم اعمال شوند)'),
    )
    ROUNDING_CHOICES = ((1, 'تومان'), (100, 'صد تومان'), (1000, 'هزار تومان'))

    promotions_enabled = models.BooleanField(
        default=True, verbose_name='تخفیف‌های خودکار فعال باشند',
        help_text='خاموش: هیچ تخفیف خودکاری روی قیمت‌ها اعمال نمی‌شود (کدهای تخفیف از این کلید مستقل‌اند).',
    )
    apply_to_vip = models.BooleanField(
        default=False, verbose_name='تخفیف‌های خودکار روی قیمت کاربران سطح ویژه هم اعمال شود',
        help_text='فقط مخصوص تخفیف‌های خودکار (Promotion)؛ روی کدهای تخفیف اثری ندارد. کاربران با سطح قیمت ۳ به بالا '
                  '(قیمت همکار). پیش‌فرض خاموش تا روی قیمت همکار تخفیف اضافه نشود و حاشیه‌ی سود منفی نشود.',
    )
    apply_for_cash = models.BooleanField(default=True, verbose_name='برای پرداخت نقدی اعمال شود')
    apply_for_check = models.BooleanField(default=True, verbose_name='برای پرداخت چکی اعمال شود')
    promotion_stacking = models.CharField(
        max_length=10, choices=STACKING_CHOICES, default=STACK_BEST, verbose_name='ترکیب تخفیف‌های هم‌پوشان',
        help_text='وقتی چند تخفیف هم‌زمان روی یک کالا مشمول‌اند.',
    )
    max_item_discount_percent = models.PositiveSmallIntegerField(
        default=90, validators=[MinValueValidator(1), MaxValueValidator(99)],
        verbose_name='سقف تخفیف روی هر کالا (درصد از قیمت پایه)',
        help_text='مجموع تخفیف‌های خودکار روی یک واحد کالا هیچ‌وقت از این درصدِ قیمت پایه بیشتر نمی‌شود.',
    )
    rounding_step = models.PositiveIntegerField(
        choices=ROUNDING_CHOICES, default=1, verbose_name='گردکردن قیمت تخفیف‌خورده',
        help_text='قیمت نهایی تخفیف‌خورده به نزدیک‌ترین مضرب این مقدار گرد می‌شود (هرگز بالاتر از قیمت پایه نمی‌رود).',
    )
    updated_at = models.DateTimeField(auto_now=True, verbose_name='آخرین ویرایش')

    class Meta:
        verbose_name = 'سیاست تخفیف'
        verbose_name_plural = 'سیاست تخفیف'

    def __str__(self):
        return 'سیاست تخفیف'

    def save(self, *args, **kwargs):
        self.pk = 1   # singleton
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        pass          # تنها ردیف سیاست نباید حذف شود

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class Promotion(models.Model):
    """
    تخفیف خودکار روی قیمت واحد کالا («شگفت‌انگیز»): درصدی، مبلغ ثابت یا قیمت ویژه، برای محصول/دسته (و زیردسته‌ها)/
    برند/کل فروشگاه (با استثنا)، در بازه‌ی زمانی و برای مخاطب مشخص.
    """
    KIND_PERCENT = 'percent'
    KIND_FIXED = 'fixed'
    KIND_SPECIAL_PRICE = 'special_price'
    KIND_CHOICES = (
        (KIND_PERCENT, 'درصدی'),
        (KIND_FIXED, 'مبلغ ثابت (کسر از قیمت هر واحد)'),
        (KIND_SPECIAL_PRICE, 'قیمت ویژه (قیمت هر واحد = مقدار)'),
    )
    PAYMENT_ANY = ''
    PAYMENT_CHOICES = ((PAYMENT_ANY, 'همه‌ی روش‌ها'), ('cash', 'فقط پرداخت نقدی'), ('check', 'فقط پرداخت چکی'))

    STATUS_INACTIVE = 'inactive'
    STATUS_SCHEDULED = 'scheduled'
    STATUS_ACTIVE = 'active'
    STATUS_EXPIRED = 'expired'
    STATUS_LABELS = {
        STATUS_INACTIVE: 'غیرفعال', STATUS_SCHEDULED: 'زمان‌بندی‌شده', STATUS_ACTIVE: 'فعال', STATUS_EXPIRED: 'منقضی',
    }

    title = models.CharField(max_length=200, verbose_name='عنوان')
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default=KIND_PERCENT, verbose_name='نوع تخفیف')
    value = models.PositiveIntegerField(
        verbose_name='مقدار', help_text='درصدی: عدد ۱ تا ۹۹؛ مبلغ ثابت و قیمت ویژه: تومان.',
    )
    max_discount_amount = models.PositiveIntegerField(
        null=True, blank=True, verbose_name='سقف مبلغ تخفیف برای هر واحد (تومان)',
        help_text='فقط برای نوع درصدی؛ خالی = بدون سقف.',
    )

    starts_at = models.DateTimeField(verbose_name='شروع')
    ends_at = models.DateTimeField(verbose_name='پایان')
    is_active = models.BooleanField(default=True, verbose_name='فعال')
    priority = models.SmallIntegerField(
        default=0, verbose_name='اولویت',
        help_text='عدد بزرگ‌تر = اولویت بیشتر (برای تساوی در حالت «بهترین» و ترتیب اعمال در حالت «جمع‌شونده»).',
    )

    # --- مخاطب و شرایط ---
    login_required = models.BooleanField(default=False, verbose_name='فقط برای کاربران واردشده')
    min_loyalty_level = models.PositiveSmallIntegerField(
        default=0, choices=LOYALTY_CHOICES, verbose_name='حداقل سطح وفاداری',
        help_text='بر اساس تعداد سفارش‌های پرداخت‌شده‌ی کاربر (نیازمند ورود).',
    )
    price_levels = models.CharField(
        max_length=40, blank=True, verbose_name='فقط برای این سطوح قیمت',
        help_text='مثلاً «1,2» (سطح قیمت ۱ و ۲)؛ خالی = همه‌ی سطوح. مهمان سطح ۱ حساب می‌شود.',
    )
    payment_method = models.CharField(max_length=10, blank=True, choices=PAYMENT_CHOICES, default=PAYMENT_ANY,
                                      verbose_name='روش پرداخت')

    # --- نمایش ---
    show_in_flash_deals = models.BooleanField(
        default=True, verbose_name='نمایش در باکس «شگفت‌انگیز»',
        help_text='فقط تخفیف‌های عمومی (بدون شرط ورود/سطح/روش پرداخت) در باکس نشان داده می‌شوند. '
                  'برای تخفیف «کل فروشگاه» معمولاً خاموش کنید.',
    )
    badge_label = models.CharField(max_length=60, blank=True, verbose_name='برچسب نمایشی (اختیاری)')

    created_at = models.DateTimeField(auto_now_add=True, verbose_name='تاریخ ایجاد')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='آخرین ویرایش')

    class Meta:
        verbose_name = 'تخفیف خودکار'
        verbose_name_plural = 'تخفیف‌های خودکار'
        ordering = ('-starts_at', '-id')

    def __str__(self):
        return self.title

    # ---------------------------------------------------------------- خواندنی‌ها
    def status(self, now=None):
        now = now or timezone.now()
        if not self.is_active:
            return self.STATUS_INACTIVE
        if now < self.starts_at:
            return self.STATUS_SCHEDULED
        if now > self.ends_at:
            return self.STATUS_EXPIRED
        return self.STATUS_ACTIVE

    @property
    def status_label(self):
        return self.STATUS_LABELS[self.status()]

    @property
    def is_public(self):
        """ بدون هیچ شرط مخاطب/روش پرداخت (فقط چنین تخفیفی در باکس عمومی «شگفت‌انگیز» تبلیغ می‌شود) """
        return not (self.login_required or self.min_loyalty_level or self.price_levels.strip() or self.payment_method)

    @property
    def value_display(self):
        if self.kind == self.KIND_PERCENT:
            return f'{self.value}٪'
        return f'{self.value:,} تومان'

    # ---------------------------------------------------------------- اعتبارسنجی
    def clean(self):
        errors = {}
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            errors['ends_at'] = 'زمان پایان باید بعد از زمان شروع باشد.'
        if self.kind == self.KIND_PERCENT:
            if not 1 <= (self.value or 0) <= 99:
                errors['value'] = 'برای تخفیف درصدی، مقدار باید بین ۱ تا ۹۹ باشد.'
        elif (self.value or 0) < 1:
            errors['value'] = 'مقدار باید بیشتر از صفر (تومان) باشد.'
        if self.max_discount_amount is not None and self.kind != self.KIND_PERCENT:
            errors['max_discount_amount'] = 'سقف مبلغ فقط برای تخفیف درصدی معنا دارد.'
        if self.max_discount_amount is not None and self.max_discount_amount < 1:
            errors['max_discount_amount'] = 'سقف مبلغ باید بیشتر از صفر باشد (یا خالی بماند).'
        levels = (self.price_levels or '').strip()
        if levels:
            parsed = parse_price_levels(levels)
            if not _PRICE_LEVELS_RE.match(levels) or not parsed or not all(1 <= n <= 10 for n in parsed):
                errors['price_levels'] = 'قالب درست: اعداد ۱ تا ۱۰ با کاما، مثل «1,2».'
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.price_levels = (self.price_levels or '').replace(' ', '')
        super().save(*args, **kwargs)


def parse_price_levels(text):
    """ «1,2,5» ← frozenset({1, 2, 5}) (مقدار نامعتبر نادیده گرفته می‌شود) """
    result = set()
    for part in (text or '').split(','):
        part = part.strip()
        if part.isdigit():
            result.add(int(part))
    return frozenset(result)


class PromotionTarget(models.Model):
    """
    یک «هدف» (شمول یا استثنا) برای تخفیف: کل فروشگاه، یک محصول، یک دسته (با/بدون زیردسته‌ها) یا یک برند.
    کالا مشمول است اگر با حداقل یک هدفِ «شمول» بخواند و با هیچ «استثنایی» نخواند.
    """
    TYPE_ALL = 'all'
    TYPE_PRODUCT = 'product'
    TYPE_CATEGORY = 'category'
    TYPE_BRAND = 'brand'
    TYPE_CHOICES = (
        (TYPE_ALL, 'کل فروشگاه'),
        (TYPE_PRODUCT, 'محصول'),
        (TYPE_CATEGORY, 'دسته‌بندی'),
        (TYPE_BRAND, 'برند'),
    )

    promotion = models.ForeignKey(Promotion, related_name='targets', on_delete=models.CASCADE, verbose_name='تخفیف')
    target_type = models.CharField(max_length=10, choices=TYPE_CHOICES, default=TYPE_PRODUCT, verbose_name='نوع هدف')
    product = models.ForeignKey(Product, null=True, blank=True, related_name='+', on_delete=models.CASCADE, verbose_name='محصول')
    category = models.ForeignKey(Category, null=True, blank=True, related_name='+', on_delete=models.CASCADE, verbose_name='دسته‌بندی')
    brand = models.ForeignKey(Brand, null=True, blank=True, related_name='+', on_delete=models.CASCADE, verbose_name='برند')
    include_descendants = models.BooleanField(
        default=True, verbose_name='شامل زیردسته‌ها',
        help_text='فقط برای هدف «دسته‌بندی».',
    )
    is_exclusion = models.BooleanField(default=False, verbose_name='استثنا (از شمول خارج شود)')

    class Meta:
        verbose_name = 'هدف تخفیف'
        verbose_name_plural = 'اهداف تخفیف'
        ordering = ('is_exclusion', 'id')
        # فقط «اتصال اضافیِ اشتباه» را ممنوع می‌کند (مثلاً نوع «محصول» که دسته هم دارد). عمداً *الزامِ* وجود
        # هدفِ درست را نمی‌گذارد: روی SQL Server وقتی محصول/دسته/برندِ هدف حذف می‌شود، جنگو (برای FK های nullable با
        # CASCADE، چون بک‌اند تعویق قیدها را ندارد) اول ستون را موقتاً NULL می‌کند و بعد ردیف را پاک می‌کند؛ قیدِ
        # «حتماً پر باشد» همین NULLِ گذرا را رد می‌کرد و حذف هر محصولِ دارای تخفیف شکست می‌خورد. الزامِ هدف در
        # clean() و فرم ادمین (PromotionTargetInline) اعمال می‌شود.
        constraints = [
            models.CheckConstraint(
                name='promotion_target_has_no_extra_links',
                condition=(
                    Q(target_type='all', product__isnull=True, category__isnull=True, brand__isnull=True)
                    | Q(target_type='product', category__isnull=True, brand__isnull=True)
                    | Q(target_type='category', product__isnull=True, brand__isnull=True)
                    | Q(target_type='brand', product__isnull=True, category__isnull=True)
                ),
            ),
        ]

    def __str__(self):
        prefix = 'استثنا: ' if self.is_exclusion else ''
        return f'{prefix}{self.describe()}'

    def describe(self):
        if self.target_type == self.TYPE_ALL:
            return 'کل فروشگاه'
        if self.target_type == self.TYPE_PRODUCT:
            return f'محصول «{self.product.name}»'
        if self.target_type == self.TYPE_CATEGORY:
            return f'دسته «{self.category.name}»' + (' و زیردسته‌ها' if self.include_descendants else '')
        return f'برند «{self.brand.name}»'

    def clean(self):
        errors = {}
        chosen = {
            self.TYPE_PRODUCT: self.product_id, self.TYPE_CATEGORY: self.category_id, self.TYPE_BRAND: self.brand_id,
        }
        if self.target_type == self.TYPE_ALL:
            if any(chosen.values()):
                errors['target_type'] = 'برای «کل فروشگاه» محصول/دسته/برند انتخاب نشود.'
            if self.is_exclusion:
                errors['is_exclusion'] = '«کل فروشگاه» نمی‌تواند استثنا باشد.'
        else:
            field = self.target_type
            if not chosen.get(self.target_type):
                errors[field] = 'برای این نوع هدف، مورد مربوطه را انتخاب کنید.'
            if any(value for key, value in chosen.items() if key != self.target_type):
                errors['target_type'] = 'فقط یکی از محصول/دسته/برند باید انتخاب شود.'
        if errors:
            raise ValidationError(errors)
