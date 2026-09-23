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

import re
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Case, DecimalField, F, Value, When
from django.db.models.functions import Round

_DIGIT_CHARS = frozenset('0123456789' + '۰۱۲۳۴۵۶۷۸۹' + '٠١٢٣٤٥٦٧٨٩')  # لاتین + فارسی + عربی
# رقمی که بلافاصله با نماد درصد همراه است («۲۰٪»، «٪20»، «30 %») بی‌خطر است، چون همان چیزی است که فیلد
# percent هم دارد و مبلغ کالا را لو نمی‌دهد؛ فقط برای تشخیص «رقمِ درصدی» در _redact_if_numeric استفاده می‌شود.
_PERCENT_TOKEN_RE = re.compile(r'[٪%]\s*[0-9۰-۹٠-٩]+|[0-9۰-۹٠-٩]+\s*[٪%]')

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


@dataclass(frozen=True)
class GuestPricingConfig:
    """ اسنپ‌شات SiteSettings.guest_* (فقط تنظیمات قیمت مهمان) """
    mode: str
    price_level: int
    adjustment_type: str
    adjustment_value: Decimal
    price_rounding_step: int          # نامش عمداً متفاوت از DiscountPolicy.rounding_step (نگاه کنید نگهبان promotions.tests.PolicyScopeGuardTests)
    hidden_message: str


_guest_config_memo = {'value': None, 'at': 0.0}
GUEST_CONFIG_MEMO_TTL = 2.0  # ثانیه؛ هم‌الگوی promotions/index.py MEMO_TTL


def guest_pricing_config():
    """
    اسنپ‌شات تنظیمات قیمت مهمان، با memo کوتاه در حافظه‌ی پروسه (هم‌الگوی promotions/index.py): در یک
    درخواست با چند کارت محصول، SiteSettings.cached() (که خودش کش ۱۵‌دقیقه‌ای Redis دارد) فقط یک‌بار
    خوانده می‌شود، نه به ازای هر محصول؛ در هیچ حالتی کوئری دیتابیس اضافه زده نمی‌شود.
    با ذخیره‌ی SiteSettings در ادمین این memo هم فوراً باطل می‌شود (products/signals.py).
    """
    now = time.monotonic()
    cached = _guest_config_memo['value']
    if cached is not None and now - _guest_config_memo['at'] < GUEST_CONFIG_MEMO_TTL:
        return cached
    from .models import SiteSettings  # وارد کردن دیرهنگام: models.py در سطح ماژول از pricing.py می‌خواند (وابستگی یک‌طرفه)
    settings_obj = SiteSettings.cached()
    config = GuestPricingConfig(
        mode=settings_obj.guest_pricing_mode,
        price_level=settings_obj.guest_price_level,
        adjustment_type=settings_obj.guest_adjustment_type,
        adjustment_value=_to_decimal(settings_obj.guest_adjustment_value),
        price_rounding_step=settings_obj.guest_price_rounding_step,
        hidden_message=settings_obj.guest_price_hidden_message,
    )
    _guest_config_memo['value'] = config
    _guest_config_memo['at'] = now
    return config


def clear_guest_pricing_memo():
    """ باطل‌سازی فوریِ memo (صدا زده می‌شود از products/signals.py با هر ذخیره‌ی SiteSettings)؛ در تست هم مفید است """
    _guest_config_memo['value'] = None
    _guest_config_memo['at'] = 0.0


def _is_guest(user):
    return user is None or not getattr(user, 'is_authenticated', False)


def is_price_hidden(user):
    """
    آیا قیمت باید برای این کاربر کاملاً پنهان بماند؟ دو مسیر مستقل، هر دو مستقل از هم چک
    می‌شوند (نه با هم OR روی یک شرط قدیمی):
      - مهمان (لاگین‌نکرده)، فقط وقتی SiteSettings.guest_pricing_mode == hide_price.
      - کاربرِ واردشده‌ای که هنوز CustomUser.can_view_prices() او False است (چرخه‌ی تأیید
        تجاری accounts؛ مستقل از UserStatus/همگام‌سازی هلو — نگاه کنید accounts/models.py).
        staff/superuser از همان can_view_prices() معاف‌اند.

    تنها منبع این تصمیم در کل پروژه؛ price_breakdown()، annotate_effective_price() و
    ProductListView (فیلتر/مرتب‌سازی بر اساس قیمت) همه از همین استفاده می‌کنند — دقیقاً همان
    اصلی که برای «مخفی‌سازی قیمت مهمان» رعایت شد، حالا برای کاربرِ تأییدنشده هم تکرار می‌شود،
    نه یک مسیر موازی و جداگانه.
    """
    if _is_guest(user):
        return guest_pricing_config().mode == GUEST_HIDE_PRICE
    return not user.can_view_prices()


_UNSET = object()
_ONE = Decimal('1')


def _to_decimal(value):
    """ تبدیل امن به Decimal؛ مقادیر float (مثلاً آبجکت تازه‌ساخته و ذخیره‌نشده) هم پوشش داده می‌شوند """
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or 0))


def _price_level(user):
    """
    سطح مؤثر: کاربر واردشده -> price_level خودش؛ مهمان -> SiteSettings.guest_price_level (پیش‌فرض ۱، یعنی
    دقیقاً همان رفتار قبلی). این تابع در سراسر پروژه (از جمله promotions/resolver.py و promotions/catalog.py)
    به‌عنوان «سطح مؤثر» خوانده می‌شود؛ همین یک تغییر کافی است تا سوییچِ اعمال تخفیف خودکار روی قیمت ویژه (سیاست
    تخفیف)، هدف سطح قیمت تخفیف‌ها و فیلتر/
    مرتب‌سازی کاتالوگ همه بدون مسیر موازی برای مهمان هم درست کار کنند.
    """
    if user is not None and getattr(user, 'is_authenticated', False):
        return getattr(user, 'price_level', 1) or 1
    return guest_pricing_config().price_level


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


def _price_for_level(product, level):
    """
    ستون قیمتِ یک سطح (۱ تا ۱۰)؛ اگر آن سطح در هلو صفر بود (پر نشده)، به قیمت سطح ۱ (چکی) برمی‌گردد.
    منبع مشترک هم برای کاربر ویژه (سطح ۳ تا ۱۰) و هم برای مهمانِ پیکربندی‌شده روی همین سطوح؛ قبلاً این
    fallback فقط در Product.get_user_price بود که مهمان را همیشه سطح ۱ فرض می‌کرد.
    """
    if level <= 1:
        return _to_decimal(product.price)
    specific = _to_decimal(getattr(product, f'price{level}', 0))
    return specific if specific > 0 else _to_decimal(product.price)


def _round_to_guest_step(price, step):
    if step <= 1:
        return price.quantize(_ONE, rounding=ROUND_HALF_UP)
    step_d = Decimal(step)
    return (price / step_d).quantize(_ONE, rounding=ROUND_HALF_UP) * step_d


def _apply_guest_adjustment(base, config):
    """
    فقط حالت «قیمت فرمولی»: سطح پایه ± تعدیل (درصدی یا مبلغ ثابت)، گرد شده به نزدیک‌ترین مضرب step.
    اگر نتیجه صفر یا منفی شد (مثلاً مبلغ ثابت کاهشی بزرگ‌تر از قیمت پایه)، به همان قیمت پایه‌ی بدون تعدیل
    برمی‌گردیم — نه یک عدد دلبخواه — دقیقاً هم‌الگوی fallbackهای دیگر همین فایل (هرگز مبلغ صفر/نامعتبر).
    """
    if config.adjustment_type == ADJUST_PERCENT:
        adjusted = base + base * config.adjustment_value / Decimal('100')
    else:
        adjusted = base + config.adjustment_value
    adjusted = _round_to_guest_step(adjusted, config.price_rounding_step)
    return adjusted if adjusted > 0 else base


def base_price(product, user, method=None):
    """
    قیمت پایه (قبل از تخفیف‌های خودکار) بر اساس روش پرداخت.
    اگر قیمت آن سطح در هلو پر نشده باشد (صفر)، به قیمت ۱ برمی‌گردیم — این fallback از ثبت فاکتور با
    مبلغ صفر جلوگیری می‌کند (قبلاً روش «نقدی» مستقیم product.price2 را برمی‌گرداند، حتی وقتی صفر بود).

    برای کاربر مهمان (لاگین‌نکرده) در حالت «قیمت فرمولی» (SiteSettings.guest_pricing_mode)، بعد از تعیین
    قیمت پایه‌ی همان سطح، تعدیل (± درصد یا مبلغ ثابت) و گرد کردن روی همین‌جا اعمال می‌شود — تک مسیر: خروجی
    همین تابع (نه یک محاسبه‌ی جدا) پایه‌ی مرحله‌ی بعدی (تخفیف‌های خودکار) در price_breakdown() قرار می‌گیرد؛
    مهمان هیچ‌وقت سفارش نمی‌دهد (سبد login required است)، پس این مسیر فقط برای نمایش استفاده می‌شود.
    """
    if method is None:
        method = default_payment_method(user)

    if method == CHECK:
        price = _to_decimal(product.price)
    elif method == CASH:
        price2 = _to_decimal(product.price2)
        price = price2 if price2 > 0 else _to_decimal(product.price)
    else:
        price = _price_for_level(product, _price_level(user))

    if _is_guest(user):
        config = guest_pricing_config()
        if config.mode == GUEST_CALCULATED_PRICE:
            price = _apply_guest_adjustment(price, config)

    return price


def _base_price_expression(user):
    """
    همان انتخابِ سطح/روشِ base_price، ولی به‌جای یک Decimal روی یک شیء، یک عبارت ORM (Case/When روی نام
    ستون‌ها) که برای *همه‌ی* ردیف‌های یک کوئری‌ست یک‌جا در خودِ دیتابیس محاسبه می‌شود؛ برای annotate_effective_price
    (فیلتر/مرتب‌سازی قیمتی کاتالوگ). سطح/روش مؤثر یک‌بار برای کل درخواست تعیین می‌شود (نه به ازای هر ردیف)،
    فقط fallbackِ «ستون آن سطح صفر است» به ازای هر محصول فرق می‌کند؛ یعنی یک عبارت ثابت، نه N کوئری.
    """
    method = default_payment_method(user)
    if method == CHECK:
        return F('price')
    if method == CASH:
        return Case(When(price2__gt=0, then=F('price2')), default=F('price'), output_field=DecimalField())
    level = _price_level(user)                              # همیشه ۳ تا ۱۰ وقتی method == VIP
    column = f'price{level}'
    return Case(When(**{f'{column}__gt': 0}, then=F(column)), default=F('price'), output_field=DecimalField())


def annotate_effective_price(queryset, user):
    """
    queryset را با ستون effective_price (Decimal) حاشیه‌نویسی می‌کند: قیمت مؤثرِ *پایه* همین کاربر/مهمان —
    سطح/روش پرداخت ← تعدیل مهمان (فقط حالت فرمولی) ← گردکردن — دقیقاً همان ترتیب base_price()، ولی یک‌جا
    در دیتابیس برای کل کوئری‌ست (بدون کوئری اضافه، بدون N+1؛ برای فیلتر بازه‌ی قیمت/مرتب‌سازی ارزان‌ترین-
    گران‌ترین در products/views.py استفاده می‌شود).

    عمداً بدون تخفیف‌های خودکار (promotions): هدف‌گیری/ترکیب‌پذیریِ آن‌ها با یک عبارت SQL ثابت قابل‌بردارسازی
    امن نیست؛ مرتب‌سازی «بیشترین تخفیف» از قبل و جداگانه با promotions/catalog.py پوشش داده شده.
    """
    base = _base_price_expression(user)
    if not _is_guest(user):
        return queryset.annotate(effective_price=base)

    config = guest_pricing_config()
    if config.mode != GUEST_CALCULATED_PRICE:
        return queryset.annotate(effective_price=base)

    if config.adjustment_type == ADJUST_PERCENT:
        adjusted = base + base * config.adjustment_value / Decimal('100')
    else:
        adjusted = base + Value(config.adjustment_value, output_field=DecimalField())
    step = config.price_rounding_step
    adjusted = Round(adjusted / step) * step if step > 1 else Round(adjusted)

    # فallbackِ «تعدیل صفر/منفی شد» به یک annotate جدا نیاز دارد تا بشود در مرحله‌ی بعد با نام آن مقایسه کرد
    # (همان چیزی که _apply_guest_adjustment با «adjusted if adjusted > 0 else base» در پایتون انجام می‌دهد)
    queryset = queryset.annotate(_guest_adjusted_price=adjusted)
    return queryset.annotate(effective_price=Case(
        When(_guest_adjusted_price__gt=0, then=F('_guest_adjusted_price')),
        default=base, output_field=DecimalField(),
    ))


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

    visible=False وقتی is_price_hidden(user) درست باشد (مهمان در حالت «مخفی‌سازی قیمت»
    SiteSettings.guest_pricing_mode='hide_price'، یا کاربر واردشده‌ی هنوز تأییدنشده):
    base/final عمداً None هستند (نه ۰ — تا هیچ قالبی حتی با فراموشیِ چک visible یک مبلغ نادرست/گمراه‌کننده
    نشان ندهد)، discount_amount صفر است و applied (اگر تخفیفی بود) بدون هیچ مبلغی، فقط برای badge_label/
    ends_at نگه داشته می‌شود؛ percent از پیش (روی مبلغ‌های واقعی، پیش از پنهان‌سازی) محاسبه و اینجا نگه‌داری
    شده چون بعد از پنهان‌سازی دیگر base/final برای محاسبه‌ی آن در دسترس نیست.
    """
    base: Decimal
    final: Decimal
    applied: tuple = ()
    visible: bool = True
    masked_percent: int = 0

    @property
    def has_discount(self):
        if not self.visible:
            return bool(self.applied)
        return bool(self.applied) and self.final < self.base

    @property
    def discount_amount(self):
        if not self.visible:
            return Decimal('0')
        return self.base - self.final

    @property
    def percent(self):
        """ درصد معادل تخفیف (برای نشان روی کارت)؛ حداقل ۱ وقتی تخفیفی هست """
        if not self.visible:
            return self.masked_percent
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


def _redact_if_numeric(text):
    """
    عنوان/نشانِ تخفیف را متنِ آزاد ادمین می‌سازد (مثلاً «۵۰ هزار تومان تخفیف» یا «شگفت‌انگیز ۹۹۰۰۰»)؛ هیچ
    تضمینی نیست که رقم داخل متن مبلغ نباشد. اما رقمی که بلافاصله با نماد درصد همراه است («۲۰٪ تخفیف ویژه»)
    بی‌خطر است، چون همان چیزی است که فیلد percent هم دارد (مبلغ کالا را لو نمی‌دهد)، پس اول این رقم‌های
    درصدی از متن حذف می‌شوند و فقط اگر رقمی *غیر از آن‌ها* باقی ماند (که می‌تواند مبلغ باشد)، کل متن اصلی
    پنهان می‌شود؛ حذفِ نیمه‌کاره (فقط ارقام مشکوک) ریسک نشتِ بخشی دارد و متنِ بریده‌بریده هم گمراه‌کننده است.
    """
    if not text:
        return ''
    remaining = _PERCENT_TOKEN_RE.sub('', text)
    if any(ch in _DIGIT_CHARS for ch in remaining):
        return ''
    return text


def _mask_applied(applied):
    """ نسخه‌ی امنِ تخفیف‌های اعمال‌شده برای مهمانِ حالت «مخفی‌سازی قیمت»: بدون هیچ مبلغ (discount/value)،
    فقط اطلاعات غیرپولی و بدون رقمِ لازم برای نشان/تایمر (badge_label، ends_at، عنوان) """
    return tuple(
        AppliedPromotion(promotion_id=a.promotion_id, title=_redact_if_numeric(a.title), kind=a.kind, value=0,
                         discount=Decimal('0'), badge_label=_redact_if_numeric(a.badge_label), ends_at=a.ends_at)
        for a in applied
    )


def _hide_price_breakdown(base, final, applied):
    """
    نسخه‌ی امن یک PriceBreakdown برای «قیمت پنهان» (مهمانِ حالت hide_price *یا* کاربرِ واردشده‌ی
    تأییدنشده — نگاه کنید is_price_hidden): درصد از روی مبلغ‌های *واقعی* (همان base/final که از
    مسیر یکپارچه‌ی معمولی، شامل تخفیف‌های خودکار، به دست آمده) یک‌بار محاسبه و نگه داشته می‌شود؛
    خودِ base/final و هر مبلغ دیگری در applied در خروجی حذف می‌شوند.
    """
    has_discount = bool(applied) and final < base
    percent = 0
    if has_discount and base > 0:
        percent = max(1, int(((base - final) * 100 / base).quantize(_ONE, rounding=ROUND_HALF_UP)))
    return PriceBreakdown(base=None, final=None, applied=_mask_applied(applied) if has_discount else (),
                          visible=False, masked_percent=percent)


def price_breakdown(product, user, method=None, discount=_UNSET, now=None):
    """
    ریز قیمت یک واحد کالا: قیمت پایه‌ی روش پرداخت (+ تعدیل مهمان در حالت فرمولی) + تخفیف‌های خودکار.

    ترتیب محاسبه (تک مسیر، بدون شاخه‌ی موازی): سطح پایه ← تعدیل مهمان (فقط calculated_price، داخل
    base_price) ← گرد کردن ← تخفیف‌های خودکار (promotions). وقتی is_price_hidden(user) درست باشد
    (مهمانِ حالت «مخفی‌سازی قیمت»، یا کاربرِ واردشده‌ی هنوز تأییدنشده)، این محاسبه عیناً همین‌جا
    کامل انجام می‌شود (درصد تخفیف درست بماند) و فقط در آخرین قدم قبل از بازگشت پنهان می‌شود.

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

    if is_price_hidden(user):
        return _hide_price_breakdown(base, final, applied)

    return PriceBreakdown(base=base, final=final, applied=tuple(applied))


def final_price(product, user, method=None, discount=_UNSET):
    """
    قیمت نهاییِ یک واحد کالا برای این کاربر: قیمت پایه‌ی روش پرداخت، منهای تخفیف‌های خودکار.

    این تابع باید تنها مسیر محاسبه‌ی قیمت در کارت محصول، صفحه‌ی محصول، سبد خرید و فاکتور باشد
    (نگاه کنید price_breakdown برای پارامترها).
    """
    return price_breakdown(product, user, method, discount).final
