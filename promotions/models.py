"""
مدل‌های تخفیف خودکار (Promotion) و سیاست سراسری تخفیف (DiscountPolicy).

تخفیف‌ها یک «لایه‌ی محاسباتی» روی قیمت‌اند و هرگز Product.price/price2..10 (سینک‌شده از هلو) را تغییر نمی‌دهند.
ترتیب محاسبه‌ی کامل قیمت در سرِ products/pricing.py مستند است.
"""

import re
import secrets

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from accounts.models import CustomUser
from locations.models import City, Province
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
    # --- ارسال رایگان (FreeShippingRule) ---
    free_shipping_rules_enabled = models.BooleanField(
        default=True, verbose_name='قاعده‌های ارسال رایگان فعال باشند',
        help_text='کلید سراسری: خاموش = هیچ قاعده‌ی ارسال رایگانی (حتی فعال‌ها) اعمال نمی‌شود. حداقل مبلغ و بازه‌ی هر قاعده '
                  'جدا در «قاعده‌های ارسال رایگان» تنظیم می‌شود. ارسال رایگانِ پرچم کالا و کد «ارسال رایگان» از این کلید مستقل‌اند.',
    )
    free_shipping_threshold_after_coupon = models.BooleanField(
        default=True, verbose_name='حداقل مبلغ سبد پس از کد تخفیفِ کالا هم سنجیده شود',
        help_text='روشن (پیش‌فرض، سود خالص سفارش را تضمین می‌کند): مبلغ سبد = پس از تخفیف خودکار و پس از کد تخفیفِ کالا. '
                  'خاموش: فقط پس از تخفیف‌های خودکار (کد تخفیف روی این شرط اثر ندارد).',
    )

    # --- کدهای تخفیف (کوپن) ---
    coupon_reservation_minutes = models.PositiveSmallIntegerField(
        default=30, validators=[MinValueValidator(5), MaxValueValidator(1440)],
        verbose_name='مهلت رزرو کد پس از ثبت سفارش (دقیقه)',
        help_text='ظرفیت کد تا پرداخت (یا پایان این مهلت) برای همان سفارش رزرو می‌ماند؛ سفارش پرداخت‌نشده بعد از مهلت، '
                  'ظرفیت را آزاد می‌کند. لغو سفارش بلافاصله آزاد می‌کند.',
    )
    coupon_max_invalid_attempts = models.PositiveSmallIntegerField(
        default=10, validators=[MinValueValidator(1), MaxValueValidator(1000)],
        verbose_name='سقف تلاش ناموفق برای وارد کردن کد (در هر بازه)',
        help_text='برای هر کاربر و هر IP جدا؛ بعد از رسیدن به سقف تا پایان بازه نمی‌تواند کد وارد کند.',
    )
    coupon_attempt_window_minutes = models.PositiveSmallIntegerField(
        default=60, validators=[MinValueValidator(1), MaxValueValidator(1440)], verbose_name='بازه‌ی شمارش تلاش ناموفق (دقیقه)',
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


# ======================================================================================== کدهای تخفیف (کوپن)
_ARABIC_INDIC = '٠١٢٣٤٥٦٧٨٩'
_PERSIAN_INDIC = '۰۱۲۳۴۵۶۷۸۹'
_CODE_STRIP_RE = re.compile(r'[\s‌‍‎‏⁠]+')
_CODE_TRANSLATION = {ord(c): str(i) for i, c in enumerate(_ARABIC_INDIC)}
_CODE_TRANSLATION.update({ord(c): str(i) for i, c in enumerate(_PERSIAN_INDIC)})
_CODE_TRANSLATION.update({ord('ـ'): None, ord('‐'): '-', ord('‑'): '-', ord('–'): '-', ord('—'): '-', ord('−'): '-'})

# الفبای کدهای تصادفی: بدون حروف/ارقام مشابه (0/O، 1/I/L) تا هنگام تایپ دستی اشتباه نشود
CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'


def normalize_code(text):
    """
    کد تخفیف را به شکل استانداردِ ذخیره/جست‌وجو درمی‌آورد: ارقام فارسی/عربی ← لاتین، حذف فاصله و نیم‌فاصله،
    خط‌تیره‌های یونیکد ← «-»، و حروف بزرگ. جست‌وجوی کد همیشه با این تابع است (بی‌حساس به حروف و نوع رقم).
    """
    text = (text or '').translate(_CODE_TRANSLATION)
    return _CODE_STRIP_RE.sub('', text).upper()


def generate_code(prefix='', length=8):
    """ کد تصادفیِ امن (secrets) با پیشوند دلخواه: «PREFIX-XXXXXXXX» """
    body = ''.join(secrets.choice(CODE_ALPHABET) for _ in range(length))
    prefix = normalize_code(prefix).strip('-')
    return f'{prefix}-{body}' if prefix else body


class Coupon(models.Model):
    """
    کد تخفیف. برخلاف Promotion (خودکار و روی قیمت واحد)، کوپن را مشتری در تسویه‌حساب وارد می‌کند و روی «مبلغ کالاها»
    (پس از تخفیف‌های خودکار) اعمال می‌شود؛ سپس ارسال جدا حساب می‌شود.

    ترکیب با تخفیف خودکار: اگر allow_with_promotions خاموش باشد (پیش‌فرض)، کوپن فقط روی اقلامِ *بدون* تخفیف خودکار
    اعمال می‌شود و اگر هیچ قلمی نماند خطای مشخص می‌دهد. کوپن «ارسال رایگان» به قیمت اقلام کاری ندارد و از این قاعده
    مستثناست. فلگ‌های DiscountPolicy (مثلاً apply_to_vip) روی کوپن اثر ندارند.
    """
    KIND_PERCENT = 'percent'
    KIND_FIXED = 'fixed'
    KIND_FREE_SHIPPING = 'free_shipping'
    KIND_CHOICES = (
        (KIND_PERCENT, 'درصدی (با سقف مبلغ)'),
        (KIND_FIXED, 'مبلغ ثابت'),
        (KIND_FREE_SHIPPING, 'ارسال رایگان (پیک درون‌شهری)'),
    )
    SCOPE_CART = 'cart'
    SCOPE_ITEMS = 'items'
    SCOPE_CHOICES = (
        (SCOPE_CART, 'کل سبد'),
        (SCOPE_ITEMS, 'فقط محصولات/دسته‌های انتخاب‌شده'),
    )
    AUDIENCE_EVERYONE = 'everyone'
    AUDIENCE_ASSIGNED = 'assigned'
    AUDIENCE_CHOICES = (
        (AUDIENCE_EVERYONE, 'همه‌ی کاربران وارد‌شده'),
        (AUDIENCE_ASSIGNED, 'فقط کاربرانی که کد برایشان تعریف شده'),
    )
    STATUS_ACTIVE, STATUS_SCHEDULED, STATUS_EXPIRED, STATUS_INACTIVE = 'active', 'scheduled', 'expired', 'inactive'
    STATUS_LABELS = {STATUS_ACTIVE: 'فعال', STATUS_SCHEDULED: 'زمان‌بندی‌شده', STATUS_EXPIRED: 'منقضی', STATUS_INACTIVE: 'غیرفعال'}

    code = models.CharField(
        max_length=40, unique=True, blank=True, verbose_name='کد',
        help_text='خالی = تولید خودکار. کد همیشه با حروف بزرگ لاتین و ارقام لاتین ذخیره می‌شود؛ مشتری با هر حالتی تایپ کند '
                  '(حروف کوچک، ارقام فارسی) معتبر است.',
    )
    title = models.CharField(max_length=200, verbose_name='عنوان')
    description = models.TextField(blank=True, default='', verbose_name='توضیح داخلی')
    kind = models.CharField(max_length=15, choices=KIND_CHOICES, default=KIND_PERCENT, verbose_name='نوع')
    value = models.PositiveIntegerField(
        default=0, verbose_name='مقدار', help_text='درصد (۱ تا ۱۰۰) یا مبلغ (تومان). برای «ارسال رایگان» نادیده گرفته می‌شود.',
    )
    max_discount_amount = models.PositiveIntegerField(
        null=True, blank=True, verbose_name='سقف مبلغ تخفیف (تومان)', help_text='فقط برای نوع درصدی.',
    )

    scope = models.CharField(max_length=10, choices=SCOPE_CHOICES, default=SCOPE_CART, verbose_name='شمول')
    products = models.ManyToManyField(Product, blank=True, related_name='+', verbose_name='محصولات مشمول')
    categories = models.ManyToManyField(
        Category, blank=True, related_name='+', verbose_name='دسته‌های مشمول', help_text='شامل همه‌ی زیردسته‌ها.',
    )
    min_cart_amount = models.PositiveIntegerField(
        default=0, verbose_name='حداقل مبلغ سبد (تومان)',
        help_text='مبلغ کل کالاهای سبد *پس از* کسر تخفیف‌های خودکار. ۰ = بدون حداقل.',
    )
    allow_with_promotions = models.BooleanField(
        default=False, verbose_name='با اقلام دارای تخفیف خودکار ترکیب شود',
        help_text='خاموش (پیش‌فرض): کوپن فقط روی اقلامِ بدون تخفیف خودکار اعمال می‌شود.',
    )

    starts_at = models.DateTimeField(null=True, blank=True, verbose_name='شروع اعتبار')
    ends_at = models.DateTimeField(null=True, blank=True, verbose_name='پایان اعتبار')
    is_active = models.BooleanField(default=True, verbose_name='فعال')

    total_limit = models.PositiveIntegerField(null=True, blank=True, verbose_name='سقف کل دفعات استفاده', help_text='خالی = نامحدود.')
    per_user_limit = models.PositiveIntegerField(null=True, blank=True, default=1, verbose_name='سقف استفاده برای هر کاربر', help_text='خالی = نامحدود.')
    first_order_only = models.BooleanField(default=False, verbose_name='فقط برای اولین خرید')
    audience = models.CharField(max_length=10, choices=AUDIENCE_CHOICES, default=AUDIENCE_EVERYONE, verbose_name='مخاطب')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='تاریخ ایجاد')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='آخرین ویرایش')

    class Meta:
        verbose_name = 'کد تخفیف'
        verbose_name_plural = 'کدهای تخفیف'
        ordering = ('-created_at', '-id')

    def __str__(self):
        return f'{self.code} — {self.title}'

    def save(self, *args, **kwargs):
        self.code = normalize_code(self.code) or self._unique_random_code()
        super().save(*args, **kwargs)

    @classmethod
    def _unique_random_code(cls):
        for _ in range(20):
            code = generate_code()
            if not cls.objects.filter(code=code).exists():
                return code
        raise RuntimeError('تولید کد یکتا ناموفق بود')

    @property
    def is_item_kind(self):
        return self.kind in (self.KIND_PERCENT, self.KIND_FIXED)

    @property
    def value_display(self):
        if self.kind == self.KIND_PERCENT:
            text = f'{self.value}٪'
            return text + (f' (حداکثر {self.max_discount_amount:,} تومان)' if self.max_discount_amount else '')
        if self.kind == self.KIND_FIXED:
            return f'{self.value:,} تومان'
        return 'ارسال رایگان'

    def status(self, now=None):
        now = now or timezone.now()
        if not self.is_active:
            return self.STATUS_INACTIVE
        if self.starts_at and now < self.starts_at:
            return self.STATUS_SCHEDULED
        if self.ends_at and now > self.ends_at:
            return self.STATUS_EXPIRED
        return self.STATUS_ACTIVE

    @property
    def status_label(self):
        return self.STATUS_LABELS[self.status()]

    def clean(self):
        errors = {}
        self.code = normalize_code(self.code)
        if self.code and not re.fullmatch(r'[A-Z0-9][A-Z0-9\-_]{2,39}', self.code):
            errors['code'] = 'کد باید ۳ تا ۴۰ نویسه‌ی لاتین/رقم (و «-» یا «_») باشد.'
        if self.kind == self.KIND_PERCENT:
            if not 1 <= self.value <= 100:
                errors['value'] = 'درصد باید بین ۱ تا ۱۰۰ باشد.'
        elif self.kind == self.KIND_FIXED:
            if self.value < 1:
                errors['value'] = 'مبلغ ثابت باید بزرگ‌تر از صفر باشد.'
        if self.max_discount_amount is not None and self.kind != self.KIND_PERCENT:
            errors['max_discount_amount'] = 'سقف مبلغ فقط برای کد درصدی معنا دارد.'
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            errors['ends_at'] = 'پایان اعتبار باید بعد از شروع باشد.'
        if self.total_limit is not None and self.total_limit < 1:
            errors['total_limit'] = 'سقف کل باید حداقل ۱ باشد (یا خالی برای نامحدود).'
        if self.per_user_limit is not None and self.per_user_limit < 1:
            errors['per_user_limit'] = 'سقف هر کاربر باید حداقل ۱ باشد (یا خالی برای نامحدود).'
        if self.kind == self.KIND_FREE_SHIPPING and self.scope != self.SCOPE_CART:
            errors['scope'] = 'کد «ارسال رایگان» به اقلام کاری ندارد؛ شمول را «کل سبد» بگذارید.'
        if errors:
            raise ValidationError(errors)


class UserCoupon(models.Model):
    """ تخصیص یک کد به یک کاربر (برای کدهای «فقط کاربران تعریف‌شده»؛ در مرحله‌ی ۴ صفحه‌ی «کدهای من» هم از همین می‌خواند) """
    SOURCE_ADMIN = 'admin'
    SOURCE_CLAIMED = 'claimed'
    SOURCE_AUTO = 'auto'
    SOURCE_CHOICES = ((SOURCE_ADMIN, 'تخصیص توسط مدیر'), (SOURCE_CLAIMED, 'دریافت توسط کاربر'), (SOURCE_AUTO, 'صدور خودکار'))

    coupon = models.ForeignKey(Coupon, on_delete=models.CASCADE, related_name='assignments', verbose_name='کد تخفیف')
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='coupon_assignments', verbose_name='کاربر')
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default=SOURCE_ADMIN, verbose_name='منبع')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='تاریخ تخصیص')

    class Meta:
        verbose_name = 'کد تخصیص‌یافته به کاربر'
        verbose_name_plural = 'کدهای تخصیص‌یافته به کاربران'
        constraints = [models.UniqueConstraint(fields=('coupon', 'user'), name='unique_coupon_per_user')]

    def __str__(self):
        return f'{self.coupon.code} ← {self.user}'


class CouponRedemption(models.Model):
    """
    مصرف یک کد در یک سفارش. چرخه: رزرو (هنگام ثبت سفارش) ← مصرف نهایی (پرداخت موفق) | آزادسازی (لغو سفارش، یا
    پایان مهلت رزرو بدون پرداخت). ظرفیت کد = مصرف‌های نهایی + رزروهای هنوز معتبر (coupons.active_uses).

    به سفارش FK ندارد (اپ‌ها وابستگی چرخه‌ای نگیرند)؛ order_id عدد ساده و یکتاست: هر سفارش حداکثر یک کد دارد.
    مبلغ‌ها اسنپ‌شات‌اند و با ویرایش/حذف بعدی کد عوض نمی‌شوند.
    """
    STATUS_RESERVED = 'reserved'
    STATUS_REDEEMED = 'redeemed'
    STATUS_RELEASED = 'released'
    STATUS_CHOICES = ((STATUS_RESERVED, 'رزرو‌شده (در انتظار پرداخت)'), (STATUS_REDEEMED, 'مصرف‌شده'), (STATUS_RELEASED, 'آزادشده'))

    coupon = models.ForeignKey(Coupon, on_delete=models.PROTECT, related_name='redemptions', verbose_name='کد تخفیف')
    user = models.ForeignKey(CustomUser, null=True, blank=True, on_delete=models.SET_NULL, related_name='coupon_redemptions', verbose_name='کاربر')
    order_id = models.PositiveIntegerField(unique=True, verbose_name='شماره سفارش')
    code = models.CharField(max_length=40, verbose_name='کد (اسنپ‌شات)')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_RESERVED, db_index=True, verbose_name='وضعیت')
    discount_amount = models.DecimalField(max_digits=12, decimal_places=0, default=0, verbose_name='تخفیف کالاها')
    shipping_discount = models.DecimalField(max_digits=12, decimal_places=0, default=0, verbose_name='تخفیف ارسال')
    reserved_at = models.DateTimeField(default=timezone.now, verbose_name='زمان رزرو')
    expires_at = models.DateTimeField(null=True, blank=True, verbose_name='پایان مهلت رزرو')
    redeemed_at = models.DateTimeField(null=True, blank=True, verbose_name='زمان مصرف نهایی')
    released_at = models.DateTimeField(null=True, blank=True, verbose_name='زمان آزادسازی')
    release_reason = models.CharField(max_length=30, blank=True, default='', verbose_name='دلیل آزادسازی')
    over_limit = models.BooleanField(default=False, verbose_name='پرداخت پس از پایان مهلت و خارج از سقف')

    class Meta:
        verbose_name = 'مصرف کد تخفیف'
        verbose_name_plural = 'مصرف کدهای تخفیف'
        ordering = ('-reserved_at', '-id')
        indexes = [models.Index(fields=['coupon', 'status'], name='redemption_coupon_status_idx')]
        constraints = [
            models.CheckConstraint(condition=models.Q(discount_amount__gte=0), name='redemption_discount_gte_0'),
            models.CheckConstraint(condition=models.Q(shipping_discount__gte=0), name='redemption_shipping_discount_gte_0'),
        ]

    def __str__(self):
        return f'{self.code} — سفارش #{self.order_id} ({self.get_status_display()})'


# ======================================================================================== قاعده‌ی ارسال رایگان
class FreeShippingRule(models.Model):
    """
    قاعده‌ی ارسال رایگان مبتنی بر سبد: حداقل مبلغ، بازه‌ی زمانی و محدوده‌ی جغرافیایی. چند قاعده هم‌زمان و زمان‌دار
    می‌توانند فعال باشند؛ هرکدام که مشمول شود کافی است. در orders/shipping.py::shipping_quote (تابع خالص) بررسی می‌شود؛
    تعرفه‌ی تنظیم‌نشده‌ی ناحیه همچنان *پیش از* هر قاعده‌ی رایگانی ارسال را مسدود می‌کند.

    مبلغ سبد = مبلغ کالاها پس از تخفیف‌های خودکار و پس از کد تخفیفِ کالا.
    محدوده: هر دو خالی = کل کشور؛ وگرنه شهرِ آدرس در «شهرها» باشد یا استانش در «استان‌ها».
    """
    POSTAGE_IGNORE = 'ignore'
    POSTAGE_COVER = 'cover'
    POSTAGE_CHOICES = (
        (POSTAGE_IGNORE, 'فقط پیک؛ ارسال با پست (پس‌کرایه) بدون تغییر'),
        (POSTAGE_COVER, 'هزینه‌ی پست را فروشگاه بپردازد (پست هم رایگان شود)'),
    )

    title = models.CharField(max_length=200, verbose_name='عنوان قاعده')
    is_active = models.BooleanField(default=True, verbose_name='فعال')
    min_cart_total = models.PositiveIntegerField(default=0, verbose_name='حداقل مبلغ سبد (تومان)', help_text='۰ = بدون حداقل.')
    starts_at = models.DateTimeField(null=True, blank=True, verbose_name='شروع', help_text='خالی = از همین حالا.')
    ends_at = models.DateTimeField(null=True, blank=True, verbose_name='پایان', help_text='خالی = بدون پایان.')
    provinces = models.ManyToManyField(Province, blank=True, related_name='+', verbose_name='استان‌ها')
    cities = models.ManyToManyField(City, blank=True, related_name='+', verbose_name='شهرها')
    postage_mode = models.CharField(max_length=10, choices=POSTAGE_CHOICES, default=POSTAGE_IGNORE, verbose_name='ارسال با پست')
    priority = models.SmallIntegerField(default=0, verbose_name='اولویت', help_text='وقتی چند قاعده مشمول است، اولویت بالاتر برچسب را تعیین می‌کند.')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='تاریخ ایجاد')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='آخرین ویرایش')

    class Meta:
        verbose_name = 'قاعده‌ی ارسال رایگان'
        verbose_name_plural = 'قواعد ارسال رایگان'
        ordering = ('-priority', 'id')

    def __str__(self):
        return self.title

    def status(self, now=None):
        now = now or timezone.now()
        if not self.is_active:
            return Coupon.STATUS_INACTIVE
        if self.starts_at and now < self.starts_at:
            return Coupon.STATUS_SCHEDULED
        if self.ends_at and now > self.ends_at:
            return Coupon.STATUS_EXPIRED
        return Coupon.STATUS_ACTIVE

    def clean(self):
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValidationError({'ends_at': 'پایان باید بعد از شروع باشد.'})
