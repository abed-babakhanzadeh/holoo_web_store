"""
تنها منبع حقیقت قیمت‌گذاری در کل پروژه.

قبل از این ماژول، قیمت در سه جای مستقل محاسبه می‌شد و نتیجه‌شان با هم نمی‌خواند:

  - کارت محصول  -> Product.get_discounted_price()   (سطح قیمت + تخفیف)
  - سبد خرید    -> CartItem.get_cost()              (سطح قیمت، بدون تخفیف!)
  - فاکتور      -> orders.views.get_order_item_price() (روش پرداخت، بدون تخفیف!)

یعنی مشتری روی کارت «۵۰٪ تخفیف» می‌دید و در سبد و فاکتور قیمت کامل پرداخت می‌کرد.
از این پس همه‌ی این مسیرها باید فقط از final_price() این فایل استفاده کنند.

این ماژول عمداً در اپ products است (پایین‌ترین لایه) تا cart و orders بتوانند بدون
ایجاد وابستگی دوطرفه از آن استفاده کنند.
"""

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


def final_price(product, user, method=None, discount=_UNSET):
    """
    قیمت نهاییِ یک واحد کالا برای این کاربر: قیمت پایه‌ی روش پرداخت، منهای تخفیف فعال.

    این تابع باید تنها مسیر محاسبه‌ی قیمت در کارت محصول، صفحه‌ی محصول، سبد خرید و فاکتور باشد.

    discount:
      - پیش‌فرض (_UNSET) -> تخفیف فعال از خود محصول خوانده می‌شود (یک کوئری)
      - None             -> عمداً بدون تخفیف حساب کن
      - یک آبجکت Discount -> از همان استفاده کن (برای وقتی از قبل prefetch شده)

    خروجی همیشه Decimal گرد‌شده به تومان صحیح است تا عددی که در قالب نمایش داده می‌شود
    (floatformat:0) دقیقاً همان عددی باشد که در OrderItem.price ذخیره و از مشتری گرفته می‌شود.
    """
    price = base_price(product, user, method)

    if discount is _UNSET:
        discount = product.active_discount

    if discount:
        price -= price * _to_decimal(discount.percent) / 100

    return price.quantize(_ONE, rounding=ROUND_HALF_UP)
