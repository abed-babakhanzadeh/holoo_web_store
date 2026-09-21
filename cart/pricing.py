"""
موتور قیمت‌گذاری سبد (CartPricing).

یک تابع خالص (بدون نوشتن در دیتابیس و بدون تکیه بر ساعت کلاینت) که فهرست ردیف‌های سبد را می‌گیرد و برای هر ردیف
و برای کل سبد سه عدد شفاف می‌دهد:

    قیمت پایه (سطح قیمت + روش پرداخت)  ←  تخفیف‌های خودکار  ←  مبلغ نهایی

منطق قیمتِ هر واحد کاملاً همان products.pricing.price_breakdown است (مرجع واحد؛ کارت محصول، سبد، صفحه‌ی تسویه و
ثبت سفارش همه از همین مسیر می‌آیند). این فایل فقط آن را روی چند ردیف اجرا می‌کند و دو تضمین اضافه می‌دهد:

  ۱. یک «اکنون» برای کل سبد: همه‌ی ردیف‌ها با *یک* لحظه‌ی سرور سنجیده می‌شوند؛ پس اگر تخفیفی درست وسط محاسبه
     منقضی شود، ردیف‌های یک سبد هرگز نصفشان با تخفیف و نصفشان بدون آن نمی‌شوند و مجموع همیشه با ردیف‌ها می‌خواند.
  ۲. مقادیر ریالیِ فاکتور/اسنپ‌شات (original / discount / final) هر ردیف از همین شیء گرفته می‌شود تا عددی که کاربر
     می‌بیند همان عددی باشد که در OrderItem و فاکتور هلو می‌نشیند.

ترتیب کامل محاسبه‌ی قیمت (پایه ← تخفیف خودکار ← کوپن ← ارسال ← جمع نهایی) در سرِ products/pricing.py مستند است؛
این موتور فقط مراحل ۱ و ۲ را (روی سطح ردیف) انجام می‌دهد. کوپن (مرحله‌ی ۳) بعد از این و روی «مبلغ کالاها»
اعمال می‌شود و ارسال جدا از این حساب می‌شود.

ساعت: `now` پیش‌فرض timezone.now() سرور است. هیچ مسیری در پروژه زمان را از درخواست/هدر/فرم کلاینت نمی‌گیرد.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from django.utils import timezone

from products.pricing import PriceBreakdown, price_breakdown, resolve_payment_method


@dataclass(frozen=True)
class PricedLine:
    """ قیمت‌گذاری یک ردیف سبد؛ همه‌ی مبلغ‌ها Decimal صحیح (ریالی) و «هر واحد» مگر اینکه صراحتاً جمع باشند """
    item: object = field(compare=False)          # خودِ CartItem (برای قالب‌ها)
    product: object = field(compare=False)
    quantity: int = 1
    breakdown: PriceBreakdown = None

    @property
    def unit_original(self):
        """ قیمت پایه‌ی هر واحد (سطح قیمت کاربر + روش پرداخت) قبل از هر تخفیف """
        return self.breakdown.base

    @property
    def unit_final(self):
        return self.breakdown.final

    @property
    def unit_discount(self):
        return self.breakdown.discount_amount

    @property
    def has_discount(self):
        return self.breakdown.has_discount

    @property
    def percent(self):
        return self.breakdown.percent

    @property
    def applied(self):
        return self.breakdown.applied

    @property
    def original_total(self):
        return self.unit_original * self.quantity

    @property
    def discount_total(self):
        return self.unit_discount * self.quantity

    @property
    def total(self):
        return self.unit_final * self.quantity


@dataclass(frozen=True)
class CartPricing:
    lines: tuple = ()
    method: str = ''
    now: object = field(default=None, compare=False)

    @property
    def is_empty(self):
        return not self.lines

    @property
    def quantity(self):
        return sum(line.quantity for line in self.lines)

    @property
    def original_total(self):
        """ جمع ردیف‌ها با قیمت پایه (قبل از تخفیف‌های خودکار) """
        return sum((line.original_total for line in self.lines), Decimal('0'))

    @property
    def promotion_discount(self):
        """ جمع تخفیف‌های خودکار همه‌ی ردیف‌ها (ریال) """
        return sum((line.discount_total for line in self.lines), Decimal('0'))

    @property
    def items_total(self):
        """ «مبلغ کالاها» پس از تخفیف‌های خودکار؛ ورودیِ مرحله‌ی کوپن و ارسال """
        return sum((line.total for line in self.lines), Decimal('0'))

    @property
    def has_discount(self):
        return self.promotion_discount > 0

    def line_for(self, item):
        """ ردیف قیمت‌گذاری‌شده‌ی یک CartItem (بر اساس شناسه) یا None """
        for line in self.lines:
            if line.item is item or (getattr(item, 'pk', None) is not None and getattr(line.item, 'pk', None) == item.pk):
                return line
        return None


def price_cart(items, user, method=None, now=None):
    """
    قیمت‌گذاری ردیف‌های سبد.

    items: هر iterable از اشیایی با .product و .quantity (CartItem)؛ ترتیب حفظ می‌شود.
    method: روش پرداخت (پیش‌فرض: روش پیش‌فرض کاربر)؛ مقدار نامعتبر/غیرمجاز طبق resolve_payment_method تصحیح می‌شود.
    now: فقط برای تست؛ پیش‌فرض ساعت سرور، یک بار برای کل سبد.
    """
    now = now or timezone.now()
    method = resolve_payment_method(user, method)
    lines = tuple(
        PricedLine(
            item=item, product=item.product, quantity=int(item.quantity),
            breakdown=price_breakdown(item.product, user, method, now=now),
        )
        for item in items
    )
    return CartPricing(lines=lines, method=method, now=now)
