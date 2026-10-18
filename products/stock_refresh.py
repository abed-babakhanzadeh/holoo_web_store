"""
تازه‌سازی موجودیِ چند کالا از منبع حسابداری، درست «لحظه‌ی خرید».

چرا: موجودی سایت از سینک کامل هلو می‌آید (هر ۲۵ دقیقه، و فقط وقتی وب‌سرویس هلو بالاست). اگر کالایی در فاصله‌ی دو سینک (مثلاً با فروش
حضوری) تمام شود، مشتری آن را می‌خرد و پرداخت می‌کند و بعد فاکتورِ هلو با خطای ۲۸ (نبود موجودی) رد می‌شود. این ماژول پیش از ثبت سفارش و
پیش از پرداخت، فقط موجودی همان کالاهای سبد/سفارش را تازه می‌کند.

products نمی‌داند منبع چیست (قرارداد رجیستری مثل reviews/purchases.py): holoo در AppConfig.ready() تأمین‌کننده را ثبت می‌کند
(holoo/stock_refresh.py). تأمین‌کننده: تابعی با امضای (products) ← دیکشنری {'refreshed': n, 'failed': m}. هر خطا یا قطعیِ هلو بی‌صدا
نادیده گرفته می‌شود و خرید با آخرین موجودیِ سینک ادامه می‌یابد (مشتری به‌خاطر کندی هلو نباید پشت درِ بسته بماند).
"""

import logging

logger = logging.getLogger(__name__)

_refresher = None


def register_refresher(func):
    global _refresher
    _refresher = func
    return func


def enabled():
    """ کلید «بررسی موجودی هنگام خرید» در تنظیمات سایت (پیش‌فرض روشن) """
    try:
        from .models import SiteSettings
        return bool(SiteSettings.cached().verify_stock_at_checkout)
    except Exception:  # noqa: BLE001 - تنظیمات در دسترس نبود: رفتار پیش‌فرض (روشن)
        return True


def refresh_products(products):
    """ موجودی محصولات را (در صورت روشن‌بودن و ثبت‌بودن تأمین‌کننده) تازه می‌کند؛ هرگز استثنا نمی‌دهد. خروجی: دیکشنری یا None """
    products = [p for p in products if p is not None]
    if _refresher is None or not products or not enabled():
        return None
    try:
        return _refresher(products)
    except Exception:  # noqa: BLE001
        logger.exception('تازه‌سازی موجودی هنگام خرید ناموفق بود؛ با موجودی سینک ادامه می‌دهیم.')
        return None
