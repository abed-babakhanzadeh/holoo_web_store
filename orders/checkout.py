"""
کمک‌های تسویه‌حساب: انتخاب آدرسِ مالک‌سنجی‌شده و محاسبه‌ی ارسال برای هر آدرس.

ورودی مرورگر فقط «شناسه‌ی آدرس» است؛ آدرس همیشه با فیلتر مالک (user=user) از دیتابیس خوانده می‌شود و کرایه/روش
ارسال را shipping_quote (orders/shipping.py) از روی داده‌ی دیتابیس می‌سازد، نه از هیچ فیلدی که مرورگر فرستاده.
"""

from dataclasses import dataclass
from decimal import Decimal

from django.utils import timezone

from accounts.models import Address
from cart.pricing import CartPricing, price_cart
from promotions import coupons, free_shipping

from .shipping import ShippingQuote, shipping_quote, waive_shipping


def get_user_address(user, raw_id):
    """
    آدرسِ متعلق به *همین کاربر* با شناسه‌ی خام (رشته/عدد) یا None.
    شناسه‌ی نامعتبر، ناموجود یا مالِ کاربر دیگر همگی None می‌دهند (از هم قابل تشخیص نیستند).
    """
    try:
        pk = int(raw_id)
    except (TypeError, ValueError):
        return None
    return (Address.objects.select_related('city', 'city__province', 'zone')
            .filter(pk=pk, user=user).first())


def address_options(user, products, site_settings, cart_total=None, free_rules=(), now=None):
    """ همه‌ی آدرس‌های کاربر، هرکدام با ShippingQuote همین سبد (برای نمایش قابل‌ارسال/مسدود بودن هر کارت) """
    products = list(products)
    return [
        {'address': address, 'quote': shipping_quote(address, products, site_settings,
                                                     cart_total=cart_total, free_rules=free_rules, now=now)}
        for address in user.addresses.select_related('city', 'city__province', 'zone')
    ]


@dataclass(frozen=True)
class CheckoutTotals:
    """
    محاسبه‌ی کامل تسویه‌حساب — تنها مرجع مبلغ‌ها (فاکتور زنده، پیام تغییر قیمت و ثبت نهایی همه از این می‌آیند):

        قیمت پایه ← تخفیف خودکار (pricing) ← کد تخفیف ← ارسال (قاعده‌های رایگان/کوپن ارسال) ← مبلغ نهایی
    """
    pricing: CartPricing
    quote: ShippingQuote
    coupon: object                       # CouponResult یا None (کدی وارد نشده)
    coupon_discount: Decimal
    now: object

    @property
    def items_total(self):
        return self.pricing.items_total

    @property
    def goods_payable(self):
        """ مبلغ کالاها پس از تخفیف خودکار و کد تخفیف """
        return self.pricing.items_total - self.coupon_discount

    @property
    def shipping_cost(self):
        return Decimal(self.quote.cost)

    @property
    def final_total(self):
        return self.goods_payable + self.shipping_cost

    @property
    def applied_coupon(self):
        return self.coupon if self.coupon is not None and self.coupon.ok else None

    @property
    def coupon_notice(self):
        """ کدِ ذخیره‌شده در نشست که در این محاسبه معتبر نبود (پیام دلیل)؛ وگرنه '' """
        return self.coupon.message if self.coupon is not None and not self.coupon.ok else ''


def compute_checkout(user, items, method, address, coupon_code, site_settings, *, now=None, lock_coupon=False):
    """
    همه‌ی اقلام، تخفیف‌ها، کد تخفیف و ارسال را از روی دیتابیس و با *یک* «اکنونِ سرور» حساب می‌کند.

    lock_coupon=True فقط داخل transaction.atomic (ثبت نهایی): ردیف کوپن قفل می‌شود و ظرفیت داخل همان قفل سنجیده می‌شود.
    ورودی مرورگر در هیچ‌کدام از مبلغ‌ها نقشی ندارد؛ coupon_code فقط یک رشته‌ی جست‌وجوست.
    """
    now = now or timezone.now()
    items = list(items)
    pricing = price_cart(items, user, method, now)
    products = [item.product for item in items]
    # سیاست سراسری (ادمین): کلید قاعده‌های ارسال رایگان و اینکه حداقل مبلغ سبد پس از کد تخفیفِ کالا هم سنجیده شود یا نه
    threshold_after_coupon = free_shipping.get_config()[1]
    rules = free_shipping.enabled_rules()

    coupon = coupons.find_coupon(coupon_code, lock=lock_coupon) if coupon_code else None
    result = None
    coupon_discount = Decimal('0')

    # کدِ کالا (درصدی/ثابت) ارسال لازم ندارد؛ مبلغ سبدِ قاعده‌های ارسال رایگان «پس از این کد» سنجیده می‌شود
    if coupon_code and coupon is not None and coupon.is_item_kind:
        result = coupons.evaluate_coupon(coupon, user, pricing, now=now)
        if result.ok:
            coupon_discount = result.item_discount

    threshold_total = pricing.items_total - (coupon_discount if threshold_after_coupon else 0)
    quote = shipping_quote(address, products, site_settings, cart_total=threshold_total, free_rules=rules, now=now)

    # کد ارسال رایگان به quoteِ بدون کوپن نیاز دارد (باید پیکِ دارای کرایه باشد) و بعد کرایه را می‌بخشد
    if coupon_code and (coupon is None or not coupon.is_item_kind):
        result = coupons.evaluate_coupon(coupon, user, pricing, base_quote=quote, now=now)
        if result.ok:
            quote = waive_shipping(quote, f'ارسال رایگان با پیک ({result.order_label})', 'coupon', coupon.id)

    return CheckoutTotals(pricing=pricing, quote=quote, coupon=result, coupon_discount=coupon_discount, now=now)
