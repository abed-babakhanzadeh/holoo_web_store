"""
گزینه‌های پرداخت تسویه حساب: تنها منبع «هر مشتری چه گزینه‌هایی می‌بیند» و «سرور چه چیزی را می‌پذیرد».

صفحه‌ی تسویه رادیوها را از available_options می‌سازد و سرور (ثبت سفارش، فاکتور زنده، به‌روزرسانی سبد) همان تابع را برای پذیرش
یا ردِ مقدار ارسالی صدا می‌زند؛ پس ظاهر و اجرا هرگز از هم جدا نمی‌شوند.

هر گزینه دو چیز را از هم جدا می‌کند: «ستون قیمت» (price_basis، همان Order.payment_method) و «روش تسویه» (settlement، Order.settlement):

  کلید        ستون قیمت  تسویه    چه کسی می‌بیند
  check       check       cheque   مشتری چکی (سطح ۱)؛ مشتری نقدی/ویژه‌ی دارای مجوز یا سیاست «چکی با قیمت مصوب»
  cash        cash        online   مشتری چکی و مشتری نقدی (سطح ۱ و ۲)
  vip         vip         online   مشتری ویژه (سطح ۳ تا ۱۰)؛ همیشه گزینه‌ی پیش‌فرض او
  vip_check   vip         cheque   مشتری ویژه با مجوز فردی یا سیاست «چکی با قیمت ویژه»
  request_check  —        —        «درخواست خرید چکی»: سفارش نمی‌سازد؛ فقط به صفحه‌ی ثبت درخواست اعتباری می‌برد (سبد دست‌نخورده)

قواعد ستون قیمت چکی در products/pricing.py::cheque_price_basis است (سیاست VIP و مجوز فردی اولویت‌دار).
"""
from dataclasses import dataclass

from products.pricing import CASH, CHECK, VIP, VIP_CHEQUE_REQUEST, _price_level, cheque_price_basis, online_price_basis, vip_cheque_policy
from products.pricing import VIP_PRICE_LEVEL

from .models import Order

OPTION_CHECK = 'check'
OPTION_CASH = 'cash'
OPTION_VIP = 'vip'
OPTION_VIP_CHECK = 'vip_check'
OPTION_REQUEST_CHECK = 'request_check'
OPTION_KEYS = frozenset({OPTION_CHECK, OPTION_CASH, OPTION_VIP, OPTION_VIP_CHECK, OPTION_REQUEST_CHECK})

KIND_ORDER = 'order'
KIND_REQUEST = 'request'

# نتیجه‌ی resolve_option
STATUS_OK = 'ok'                # همان گزینه‌ی درخواستی مجاز است
STATUS_REQUEST = 'request'      # «درخواست خرید چکی» انتخاب شده؛ سفارش ساخته نمی‌شود
STATUS_DENIED = 'denied'        # گزینه‌ی شناخته‌شده ولی برای این کاربر غیرمجاز (مثلاً چکیِ مشتری نقدی)
STATUS_DEFAULT = 'default'      # مقدار خالی/ناشناخته ← گزینه‌ی پیش‌فرض


@dataclass(frozen=True)
class PaymentOption:
    key: str
    label: str
    price_basis: str            # check / cash / vip (None برای درخواست چکی)
    settlement: str             # online / cheque (None برای درخواست چکی)
    kind: str = KIND_ORDER
    hint: str = ''

    @property
    def is_cheque(self):
        return self.settlement == Order.SETTLEMENT_CHEQUE


@dataclass(frozen=True)
class Resolution:
    option: PaymentOption
    status: str


def _cheque_option(user, basis):
    if basis == VIP:
        return PaymentOption(OPTION_VIP_CHECK, 'پرداخت چکی (با تعرفه ویژه شما)', VIP, Order.SETTLEMENT_CHEQUE,
                             hint='قیمت اختصاصی شما حفظ می‌شود؛ تسویه طبق روال چکی انجام می‌شود.')
    if _price_level(user) >= VIP_PRICE_LEVEL:
        return PaymentOption(OPTION_CHECK, 'پرداخت چکی (با قیمت مصوب چکی)', CHECK, Order.SETTLEMENT_CHEQUE,
                             hint='در این روش قیمت مصوب چکی اعمال می‌شود، نه قیمت ویژه‌ی شما.')
    return PaymentOption(OPTION_CHECK, 'پرداخت چکی (ثبت در سیستم اعتباری)', CHECK, Order.SETTLEMENT_CHEQUE)


def _online_option(user):
    if online_price_basis(user) == VIP:
        return PaymentOption(OPTION_VIP, 'پرداخت نقدی / آنلاین (با تعرفه ویژه شما)', VIP, Order.SETTLEMENT_ONLINE)
    return PaymentOption(OPTION_CASH, 'پرداخت نقدی', CASH, Order.SETTLEMENT_ONLINE)


def _request_option():
    return PaymentOption(OPTION_REQUEST_CHECK, 'درخواست خرید چکی', None, None, KIND_REQUEST,
                         hint='برای خرید چکی ابتدا درخواست اعتباری ثبت می‌شود و پس از تأیید مدیر، گزینه‌ی چکی برای شما فعال خواهد شد. '
                              'سبد خرید شما حفظ می‌شود.')


def _wants_request_option(user):
    """ «درخواست خرید چکی» فقط وقتی چکی مستقیم ندارد: مشتری نقدی (سطح ۲)، یا ویژه با سیاست request_check """
    level = _price_level(user)
    if level >= VIP_PRICE_LEVEL:
        return vip_cheque_policy() == VIP_CHEQUE_REQUEST
    return level == 2


def available_options(user):
    """ همه‌ی گزینه‌های این کاربر به ترتیب نمایش؛ گزینه‌ی پیش‌فرض (default_option) همیشه یکی از گزینه‌های سفارش‌ساز است """
    online = _online_option(user)
    basis = cheque_price_basis(user)
    cheque = _cheque_option(user, basis) if basis else None
    if cheque is not None and _price_level(user) == 1:
        return [cheque, online]                              # مشتری چکی: چکی پیش‌فرض، نقدی دوم
    options = [online]
    if cheque is not None:
        options.append(cheque)
    elif _wants_request_option(user):
        options.append(_request_option())
    return options


def order_options(user):
    return [o for o in available_options(user) if o.kind == KIND_ORDER]


def request_option(user):
    return next((o for o in available_options(user) if o.kind == KIND_REQUEST), None)


def cheque_order_option(user):
    """ گزینه‌ی سفارش‌سازِ چکیِ این کاربر (check یا vip_check) یا None """
    return next((o for o in order_options(user) if o.is_cheque), None)


def online_fallback_option(user):
    """ نخستین گزینه‌ی غیرچکی (نقدی/ویژه) که وقتی چکی به‌دلیل سقف بسته است جایگزین انتخاب می‌شود؛ None اگر نیست """
    return next((o for o in order_options(user) if not o.is_cheque), None)


def default_option(user):
    return order_options(user)[0]


def resolve_option(user, requested):
    """
    مقدار ارسالیِ فرم ← (گزینه، وضعیت). مقدار خالی/ناشناخته به گزینه‌ی پیش‌فرض می‌رود؛ گزینه‌ی شناخته‌شده‌ی غیرمجاز
    (مثلاً «check» برای مشتری نقدی بدون مجوز، یا «vip_check» برای کسی که مجوز ندارد) هرگز پذیرفته نمی‌شود (STATUS_DENIED) و
    گزینه‌ی پیش‌فرض برمی‌گردد؛ ویوی ثبت سفارش آن را با خطا رد می‌کند و بقیه‌ی ویوها (محاسبه‌ی زنده) روی پیش‌فرض می‌مانند.
    """
    requested = (requested or '').strip()
    by_key = {o.key: o for o in available_options(user)}
    chosen = by_key.get(requested)
    if chosen is not None:
        return Resolution(chosen, STATUS_REQUEST if chosen.kind == KIND_REQUEST else STATUS_OK)
    status = STATUS_DENIED if requested in OPTION_KEYS else STATUS_DEFAULT
    return Resolution(default_option(user), status)
