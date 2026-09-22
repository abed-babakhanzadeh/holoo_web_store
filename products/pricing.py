"""
تنها منبع حقیقت قیمت‌گذاری در کل پروژه.

قبل از این ماژول، قیمت در سه جای مستقل محاسبه می‌شد و نتیجه‌شان با هم نمی‌خواند:

  - کارت محصول  -> Product.get_discounted_price()   (سطح قیمت + تخفیف)
  - سبد خرید    -> CartItem.get_cost()              (سطح قیمت، بدون تخفیف!)
  - فاکتور      -> orders.views.get_order_item_price() (روش پرداخت، بدون تخفیف!)

یعنی مشتری روی کارت «۵۰٪ تخفیف» می‌دید و در سبد و فاکتور قیمت کامل پرداخت می‌کرد.
از این پس همه‌ی این مسیرها باید فقط از final_price() / price_breakdown() این فایل استفاده کنند.

ترتیب محاسبه‌ی مبلغ (هر مرحله فقط به خروجی مرحله‌ی قبل تکیه دارد):

  ۱. قیمت پایه           base_price(): ستون قیمتِ سطح قیمت کاربر و روش پرداخت (چکی/نقدی/ویژه)؛ همان
                         قیمت‌های سینک‌شده از هلو، هرگز تغییر نمی‌کنند.
  ۲. تخفیف‌های خودکار    اپ promotions از طریق register_promotion_resolver: سیاست سراسری (کاربر ویژه، روش
                         پرداخت، ترکیب‌پذیری «بهترین/جمع‌شونده»، سقف و گرد کردن)، بازه‌ی زمانی، اولویت و
                         هدف (محصول/دسته و زیردسته/برند/کل فروشگاه با استثنا) روی قیمت واحد.
  ۳. کد تخفیف (مرحله‌ی ۳ برنامه): روی سبد، *بعد* از اعتبارسنجی شرایط و حداقل مبلغ سبدِ پس از مرحله‌ی ۲.
  ۴. هزینه‌ی ارسال       orders/shipping.py با قواعد مستقل حمل‌ونقل (تخفیف درصدی هرگز روی کرایه اعمال نمی‌شود).
  ۵. مبلغ نهایی سفارش    جمع ردیف‌ها منهای تخفیف کد، به‌علاوه‌ی کرایه.

این ماژول عمداً در اپ products است (پایین‌ترین لایه) تا cart و orders بتوانند بدون
ایجاد وابستگی دوطرفه از آن استفاده کنند؛ اپ promotions هم وابستگی معکوس نمی‌سازد و فقط در
ready() خودش را ثبت می‌کند (همان الگوی products/blog_posts.py).
"""

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

# --- روش‌های پرداخت (نگاشت واقعی سطوح قیمت هلو طبق کارفرما) ---
CHECK = 'check'  # چکی -> price
CASH = 'cash'    # نقدی -> price2
VIP = 'vip'      # ویژه -> سطح قیمت ثبت‌شده‌ی خود کاربر (price3..price10)

PAYMENT_METHODS = (
    (CHECK, 'چکی (قیمت 1)'),
    (CASH, 'نقدی (قیمت 2)'),
    (VIP, 'ویژه (قیمت 3)'),
)
VALID_PAYMENT_METHODS = frozenset(key for key, _ in PAYMENT_METHODS)

# از این سطح به بالا کاربر «ویژه» محسوب می‌شود: قیمت اختصاصی خودش (price3..price10) را
# می‌گیرد و باکس انتخاب روش پرداخت برایش نمایش داده نمی‌شود.
# نکته: قبلاً فقط سطح دقیقاً ۳ ویژه حساب می‌شد؛ در نتیجه کاربر سطح ۴ تا ۱۰ در سبد priceN
# خودش را می‌دید ولی فاکتورش با price1 (چکی) ثبت می‌شد. با >= این ناسازگاری بسته می‌شود.
VIP_PRICE_LEVEL = 3

# --- قیمت برای کاربر مهمان (لاگین‌نکرده)؛ تنظیم ادمین در SiteSettings.guest_* ---
# فقط ثابت‌ها اینجا تعریف می‌شوند تا مدل و منطق قیمت‌گذاری یک منبع مشترک داشته باشند.
# ترتیب واحد محاسبه: تعیین سطح پایه ← اعمال تعدیل (فقط حالت فرمولی) ← تخفیف‌های خودکار (promotions).
GUEST_HIDE_PRICE = 'hide_price'                    # قیمت پنهان؛ فقط راهنمای ورود
GUEST_PRICE_LEVEL = 'price_level'                  # قیمت یکی از سطوح ده‌گانه (پیش‌فرض؛ همان رفتار قبلی با سطح ۱)
GUEST_CALCULATED_PRICE = 'calculated_price'        # قیمت یک سطح ± درصد یا مبلغ ثابت

GUEST_PRICING_MODES = (
    (GUEST_HIDE_PRICE, 'مخفی‌سازی قیمت (مهمان قیمتی نمی‌بیند و برای مشاهده‌ی قیمت باید وارد شود)'),
    (GUEST_PRICE_LEVEL, 'نمایش یکی از قیمت‌های ده‌گانه'),
    (GUEST_CALCULATED_PRICE, 'قیمت فرمولی (یک سطح قیمت ± درصد یا مبلغ ثابت)'),
)

ADJUST_PERCENT = 'percent'
ADJUST_FIXED = 'fixed'
ADJUSTMENT_TYPES = (
    (ADJUST_PERCENT, 'درصدی'),
    (ADJUST_FIXED, 'مبلغ ثابت (تومان)'),
)
# تعدیل درصدی: کاهش تا ۹۰٪ (قیمت هیچ‌وقت نزدیک صفر یا منفی نشود) و افزایش تا ۵۰۰٪
GUEST_PERCENT_MIN = Decimal('-90')
GUEST_PERCENT_MAX = Decimal('500')

GUEST_ROUNDING_STEPS = ((1, 'تومان'), (100, 'صد تومان'), (1000, 'هزار تومان'))

GUEST_HIDDEN_MESSAGE_DEFAULT = 'جهت مشاهده قیمت‌ها و خرید وارد شوید'


def price_level_choices():
    """ سطوح قیمت ۱ تا ۱۰ با معنای واقعی‌شان (۱ چکی، ۲ نقدی، ۳ تا ۱۰ ویژه)؛ برای فیلدهای انتخابی ادمین """
    labels = {1: 'چکی', 2: 'نقدی'}
    return [(level, f'سطح {level} — {labels.get(level, "ویژه")}') for level in range(1, 11)]


_UNSET = object()
_ONE = Decimal('1')


def _to_decimal(value):
    """ تبدیل امن به Decimal؛ مقادیر float (مثلاً آبجکت تازه‌ساخته و ذخیره‌نشده) هم پوشش داده می‌شوند """
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or 0))


def _price_level(user):
    if user is None or not getattr(user, 'is_authenticated', False):
        return 1
    return getattr(user, 'price_level', 1) or 1


def default_payment_method(user):
    """
    روش پرداختی که سبد خرید (قبل از رسیدن به صفحه‌ی تسویه) باید با آن قیمت بزند، تا عددی که
    کاربر در سبد می‌بیند همان چیزی باشد که در فاکتورش ثبت می‌شود.
    سطح ۱ -> چکی، سطح ۲ -> نقدی، بقیه‌ی سطوح (۳ تا ۱۰) -> ویژه‌ی خودشان.
    """
    level = _price_level(user)
    if level == 1:
        return CHECK
    if level == 2:
        return CASH
    return VIP


def resolve_payment_method(user, requested_method):
    """
    روش پرداخت واقعی را برمی‌گرداند:
      - کاربر ویژه همیشه روی VIP قفل است (باکس انتخاب اصلاً برایش نمایش داده نمی‌شود)
      - مقدار نامعتبر/دستکاری‌شده از فرم به روش پیش‌فرض خود کاربر برمی‌گردد، نه به یک
        شاخه‌ی else ناخواسته (قبلاً هر رشته‌ی بی‌ربطی بی‌سروصدا price2 حساب می‌شد)
    """
    if _price_level(user) >= VIP_PRICE_LEVEL:
        return VIP
    if requested_method in VALID_PAYMENT_METHODS:
        return requested_method
    return default_payment_method(user)


def base_price(product, user, method=None):
    """
    قیمت پایه (قبل از تخفیف) بر اساس روش پرداخت.
    اگر قیمت آن سطح در هلو پر نشده باشد (صفر)، به قیمت ۱ برمی‌گردیم — همان رفتاری که
    Product.get_user_price از قبل داشت. این fallback از ثبت فاکتور با مبلغ صفر جلوگیری می‌کند
    (قبلاً روش «نقدی» مستقیم product.price2 را برمی‌گرداند، حتی وقتی صفر بود).
    """
    if method is None:
        method = default_payment_method(user)

    if method == CHECK:
        return _to_decimal(product.price)

    if method == CASH:
        price2 = _to_decimal(product.price2)
        return price2 if price2 > 0 else _to_decimal(product.price)

    return _to_decimal(product.get_user_price(user))


@dataclass(frozen=True)
class AppliedPromotion:
    """ یک تخفیف خودکارِ اعمال‌شده روی قیمت واحد (خروجی resolver اپ promotions) """
    promotion_id: int
    title: str
    kind: str
    value: int
    discount: Decimal          # مبلغ کم‌شده از قیمت واحد توسط همین تخفیف
    badge_label: str = ''
    ends_at: object = None


@dataclass(frozen=True)
class PriceBreakdown:
    """
    ریز قیمت یک واحد کالا برای یک کاربر: قیمت پایه، قیمت نهایی و تخفیف‌های خودکار اعمال‌شده.
    قالب‌ها به‌جای پرس‌وجوی جدا برای تخفیف، فقط همین را می‌خوانند تا نمایش و مبلغ پرداختی یکی بماند.
    """
    base: Decimal
    final: Decimal
    applied: tuple = ()

    @property
    def has_discount(self):
        return bool(self.applied) and self.final < self.base

    @property
    def discount_amount(self):
        return self.base - self.final

    @property
    def percent(self):
        """ درصد معادل تخفیف (برای نشان روی کارت)؛ حداقل ۱ وقتی تخفیفی هست """
        if not self.has_discount or self.base <= 0:
            return 0
        return max(1, int((self.discount_amount * 100 / self.base).quantize(_ONE, rounding=ROUND_HALF_UP)))

    @property
    def promotion(self):
        """ تخفیف اصلی (اولین اعمال‌شده) یا None """
        return self.applied[0] if self.applied else None

    @property
    def badge_label(self):
        return next((a.badge_label for a in self.applied if a.badge_label), '')

    @property
    def ends_at(self):
        """ نزدیک‌ترین پایان بین تخفیف‌های اعمال‌شده (برای تایمر) """
        ends = [a.ends_at for a in self.applied if a.ends_at]
        return min(ends) if ends else None


# resolver تخفیف خودکار؛ اپ promotions در ready() ثبت می‌کند:
#   resolver(product, user, method, base_price, now) -> (final_price: Decimal, applied: tuple[AppliedPromotion])
# اگر ثبت نشده باشد (یا اپ نصب نباشد) هیچ تخفیفی اعمال نمی‌شود.
_promotion_resolver = None


def register_promotion_resolver(resolver):
    global _promotion_resolver
    _promotion_resolver = resolver


def price_breakdown(product, user, method=None, discount=_UNSET, now=None):
    """
    ریز قیمت یک واحد کالا: قیمت پایه‌ی روش پرداخت + تخفیف‌های خودکار.

    discount:
      - پیش‌فرض (_UNSET) -> تخفیف‌های خودکار از اپ promotions محاسبه می‌شود
      - None             -> عمداً بدون تخفیف حساب کن

    خروجی‌ها Decimal گرد‌شده به تومان صحیح‌اند تا عددی که در قالب نمایش داده می‌شود
    (floatformat:0) دقیقاً همان عددی باشد که در OrderItem.price ذخیره و از مشتری گرفته می‌شود.
    """
    if method is None:
        method = default_payment_method(user)
    base = base_price(product, user, method).quantize(_ONE, rounding=ROUND_HALF_UP)

    if discount is not _UNSET and discount is not None:
        raise TypeError('discount فقط می‌تواند None یا مقدار پیش‌فرض باشد؛ تخفیف‌ها از اپ promotions می‌آیند.')

    final, applied = base, ()
    if discount is _UNSET and _promotion_resolver is not None and base > 0:
        final, applied = _promotion_resolver(product, user, method, base, now)
        final = final.quantize(_ONE, rounding=ROUND_HALF_UP)
        if final >= base or final < 0:
            final, applied = base, ()
    return PriceBreakdown(base=base, final=final, applied=tuple(applied))


def final_price(product, user, method=None, discount=_UNSET):
    """
    قیمت نهاییِ یک واحد کالا برای این کاربر: قیمت پایه‌ی روش پرداخت، منهای تخفیف‌های خودکار.

    این تابع باید تنها مسیر محاسبه‌ی قیمت در کارت محصول، صفحه‌ی محصول، سبد خرید و فاکتور باشد
    (نگاه کنید price_breakdown برای پارامترها).
    """
    return price_breakdown(product, user, method, discount).final
