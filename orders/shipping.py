"""
محاسبه‌ی هزینه‌ی ارسال سفارش — تابع خالص `shipping_quote`.

ورودی: آدرس انتخاب‌شده، کالاهای سبد و تنظیمات سایت. خروجی: یک ShippingQuote (بدون هیچ اثر جانبی).
ویو/فرم/تسک هلو هیچ قاعده‌ی حمل را در خودشان نمی‌نویسند؛ فقط این خروجی را می‌خوانند، پس همه‌ی
قواعد و پیام‌ها یک‌جا (و قابل‌تست) هستند. ورودی مرورگر هرگز مبلغ نیست: ویو فقط شناسه‌ی آدرس را می‌گیرد،
آدرس را با فیلتر مالک از دیتابیس می‌خواند و همین تابع را روی آن صدا می‌زند.

ماتریس تصمیم (به ترتیب):
  ۱. آدرس نیست                                   ← غیرممکن  (no_address)
  ۲. آدرس ناحیه دارد
       ناحیه غیرفعال شده                         ← غیرممکن  (zone_inactive)
       تعرفه‌ی ناحیه ۰ (تنظیم‌نشده)              ← غیرممکن  (tariff_unset) — *پیش از* هر تخفیفی
       سبدِ «ارسال رایگان» و سیاست سایت روشن     ← پیک، ۰ تومان (برچسب «ارسال رایگان با پیک»)
       قاعده‌ی ارسال رایگان (FreeShippingRule)   ← پیک، ۰ تومان (برچسب «ارسال رایگان (خرید بالای X تومان)»)
       وگرنه                                     ← پیک، مبلغ = تعرفه‌ی ناحیه
  ۳. آدرس ناحیه ندارد
       شهر ناحیه‌ی فعال دارد                     ← غیرممکن  (zone_required)
       پس‌کرایه غیرفعال                          ← غیرممکن  (postage_disabled)
       قاعده‌ی رایگانِ «هزینه‌ی پست با فروشگاه»  ← پست، ۰ تومان با برچسب «ارسال رایگان با پست (…)»
       وگرنه                                     ← پست، ۰ تومان با برچسب «پس‌کرایه …» (ردیفی به هلو نمی‌رود)

قاعده‌های ارسال رایگان (free_rules) *اسنپ‌شات‌های خالص* هستند که اپ promotions می‌سازد (این ماژول به هیچ اپ دیگری
وابسته نیست): هر قاعده متد matches(city, cart_total, now) و label()/post_label() دارد. cart_total = مبلغ کالاها پس از
تخفیف‌های خودکار و کد تخفیفِ کالا؛ اگر None داده شود قاعده‌ها نادیده گرفته می‌شوند. کوپن «ارسال رایگان» بعد از
شکل‌گیری quote با waive_shipping اعمال می‌شود (تعرفه‌ی صفر همچنان *پیش از* آن مسدود می‌کند).

چرا تعرفه‌ی تنظیم‌نشده بالاتر از «ارسال رایگان» است: ناحیه‌ای که تعرفه ندارد هنوز از نظر ناوگان لجستیک برای ارسال
با پیک آماده و تأیید نشده؛ پس حتی وقتی کرایه‌اش صفر می‌شود (سبد رایگان + سیاست روشن) سفارش نباید ثبت شود.

«سبدِ ارسال رایگان» یعنی همه‌ی کالاهای سبد برچسب free_shipping دارند (قاعده‌ی قبلی پروژه: هزینه‌ی ارسال
به‌ازای کل مرسوله است، پس یک کالای غیررایگان کافی است تا کرایه‌ی کامل گرفته شود).
"""

from dataclasses import dataclass, replace

from django.utils import timezone

COURIER = 'courier'
POST = 'post'
SHIPPING_METHODS = (COURIER, POST)

REASON_NO_ADDRESS = 'no_address'
REASON_ZONE_REQUIRED = 'zone_required'
REASON_ZONE_INACTIVE = 'zone_inactive'
REASON_TARIFF_UNSET = 'tariff_unset'
REASON_POSTAGE_DISABLED = 'postage_disabled'

MSG_NO_ADDRESS = 'برای ثبت سفارش یک آدرس تحویل انتخاب کنید.'
MSG_ZONE_REQUIRED = 'برای شهر این آدرس انتخاب ناحیه الزامی است؛ لطفاً آدرس را ویرایش و ناحیه را مشخص کنید.'
MSG_ZONE_INACTIVE = 'ارسال به ناحیه‌ی این آدرس فعلاً انجام نمی‌شود؛ لطفاً آدرس را ویرایش کنید یا با پشتیبانی تماس بگیرید.'
MSG_TARIFF_UNSET = 'تعرفه ارسال به این ناحیه هنوز تعیین نشده است؛ لطفاً با پشتیبانی تماس بگیرید.'

LABEL_COURIER = 'ارسال با پیک'
LABEL_COURIER_FREE = 'ارسال رایگان با پیک'


@dataclass(frozen=True)
class ShippingQuote:
    """
    نتیجه‌ی محاسبه‌ی ارسال.

    available  : آیا سفارش با این آدرس قابل ارسال است (False ← ثبت سفارش باید مسدود شود)
    method     : 'courier' (پیک درون‌شهری) | 'post' (پست، پس‌کرایه) | '' (وقتی available=False)
    cost       : مبلغی که به فاکتور اضافه می‌شود (تومان)؛ برای پست و حالت‌های غیرممکن همیشه ۰
    label      : متن نمایشی (مثلاً «ارسال با پیک»، «پس‌کرایه (پرداخت هزینه درب منزل)»)؛ وقتی available=False خالی
    reason     : کد دلیل مسدود شدن (REASON_*) یا '' وقتی available=True
    message    : پیام قابل‌نمایش به کاربر وقتی available=False؛ وگرنه ''
    free_cart  : آیا سبد «ارسال رایگان» بود (همه‌ی کالاها پرچم داشتند)
    free_source: چرا رایگان شد: '' | 'cart' (پرچم کالاها) | 'rule' (FreeShippingRule) | 'coupon' (کد ارسال رایگان)
    free_source_id: شناسه‌ی قاعده/کوپن برای منبع بالا (وگرنه None)
    waived_cost: مبلغ کرایه‌ای که بخشیده شد (تعرفه‌ی پیک)؛ برای پست همیشه ۰
    """
    available: bool
    method: str
    cost: int
    label: str
    reason: str
    message: str
    free_cart: bool
    free_source: str = ''
    free_source_id: int = None
    waived_cost: int = 0

    @property
    def is_courier(self):
        return self.method == COURIER

    @property
    def is_postage_collect(self):
        return self.method == POST


def _blocked(reason, message, free_cart):
    return ShippingQuote(available=False, method='', cost=0, label='', reason=reason, message=message, free_cart=free_cart)


def is_free_shipping_cart(products):
    """ همه‌ی کالاهای سبد «ارسال رایگان» دارند (سبد خالی رایگان حساب نمی‌شود) """
    products = list(products)
    return bool(products) and all(p.free_shipping for p in products)


def _matching_rule(free_rules, city, cart_total, now):
    """ اولین قاعده‌ی مشمول (فهرست از قبل به‌ترتیب اولویت است)؛ بدون مبلغ سبد، هیچ قاعده‌ای مشمول نیست """
    if cart_total is None:
        return None
    for rule in free_rules:
        if rule.matches(city, cart_total, now):
            return rule
    return None


def waive_shipping(quote, label, source, source_id=None):
    """
    کرایه‌ی یک quoteِ قابل‌ارسال را می‌بخشد (کوپن ارسال رایگان). فقط پیکِ دارای کرایه بخشیده می‌شود؛ روی quoteِ
    مسدود، پست، یا پیکِ از قبل رایگان همان quote برمی‌گردد.
    """
    if not quote.available or quote.method != COURIER or quote.cost <= 0:
        return quote
    return replace(quote, cost=0, label=label, free_source=source, free_source_id=source_id, waived_cost=quote.cost)


def shipping_quote(address, products, site_settings, *, cart_total=None, free_rules=(), now=None):
    """
    هزینه/روش ارسال را برای (آدرس، سبد، تنظیمات) برمی‌گرداند.

    address       : accounts.Address (ذخیره‌شده یا نه) یا None
    products      : iterable از کالاهای سبد (فقط پرچم free_shipping خوانده می‌شود)
    site_settings : products.SiteSettings (یا هر شیئی با فیلدهای سیاست حمل)
    cart_total    : مبلغ کالاها پس از تخفیف‌ها؛ فقط برای قاعده‌های ارسال رایگان (None = قاعده‌ها نادیده)
    free_rules    : اسنپ‌شات‌های FreeShippingRule به‌ترتیب اولویت
    now           : ساعت سرور؛ پیش‌فرض timezone.now() (هرگز ساعت کلاینت)
    """
    free_cart = is_free_shipping_cart(products)
    now = now or timezone.now()

    if address is None:
        return _blocked(REASON_NO_ADDRESS, MSG_NO_ADDRESS, free_cart)

    zone = address.zone if address.zone_id else None

    # --- آدرسِ ناحیه‌دار: ارسال با پیک ---
    if zone is not None:
        if not zone.is_active:
            return _blocked(REASON_ZONE_INACTIVE, MSG_ZONE_INACTIVE, free_cart)
        if not zone.has_tariff:
            return _blocked(REASON_TARIFF_UNSET, MSG_TARIFF_UNSET, free_cart)
        tariff = int(zone.shipping_cost)
        if free_cart and site_settings.courier_free_for_free_shipping_cart:
            return ShippingQuote(True, COURIER, 0, LABEL_COURIER_FREE, '', '', free_cart, 'cart', None, tariff)
        rule = _matching_rule(free_rules, address.city, cart_total, now)
        if rule is not None:
            return ShippingQuote(True, COURIER, 0, rule.label(), '', '', free_cart, 'rule', rule.id, tariff)
        return ShippingQuote(True, COURIER, tariff, LABEL_COURIER, '', '', free_cart)

    # --- بدون ناحیه ---
    if address.city.active_zones().exists():
        return _blocked(REASON_ZONE_REQUIRED, MSG_ZONE_REQUIRED, free_cart)

    # --- سایر شهرها: پست با پس‌کرایه (مبلغی به فاکتور اضافه نمی‌شود) ---
    if not site_settings.postage_collect_enabled:
        return _blocked(REASON_POSTAGE_DISABLED, site_settings.postage_disabled_message, free_cart)
    rule = _matching_rule(free_rules, address.city, cart_total, now)
    if rule is not None and rule.covers_postage:
        return ShippingQuote(True, POST, 0, rule.post_label(), '', '', free_cart, 'rule', rule.id, 0)
    return ShippingQuote(True, POST, 0, site_settings.postage_collect_label, '', '', free_cart)
