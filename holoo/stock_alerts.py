"""
«اطلاع بده وقتی موجود شد»: ارسال اعلان، بدون هیچ بار اضافه روی هلو.

حسابرسی ۱۷ اکتبر ۲۰۲۶: کارفرما کالای ناموجودی را «پیامک بده وقتی موجود شد» زد و دو دقیقه بعد در هلو موجودی را ۰ به ۱۰ رساند؛ پیامکی
نیامد، چون موجودی تازه فقط با سینک کامل (هر ۲۵ دقیقه، و فقط وقتی وب‌سرویس هلو بالاست) دیده می‌شود. تصمیم کارفرما: تأخیر (حتی یک ساعت)
مهم نیست، *رسیدنِ پیام* مهم است و نباید به‌ازای هر درخواست به هلو تماس گرفت (هزاران درخواست = بار بی‌خود). پس:

  - تشخیص موجودی تازه همان سینک کامل است (هیچ تماس اضافه‌ای به هلو نیست)؛
  - sweep_stock_alerts (هر ۱۰ دقیقه، فقط دیتابیس خودِ سایت) *بر پایه‌ی وضعیت* کار می‌کند، نه «گذار»: هر درخواست در انتظار که کالایش الان
    قابل‌فروش است (موجودی − رزرو − بافر) اعلان می‌گیرد. پس هیچ پیامی گم نمی‌شود، حتی اگر سیگنالِ سینک به هر دلیل (قطعی هلو، خطای
    شنونده، ری‌استارت Worker، آزادشدن رزروِ سفارش لغوشده) از دست رفته باشد.
"""

import logging

from django.db.models import Exists, OuterRef

from products.stock import available_of

logger = logging.getLogger(__name__)

CHUNK = 500


def send_back_in_stock(product_ids):
    """ سیگنال «دوباره موجود شد» برای محصولات؛ شنونده (notifications/receivers.py) پیام را برای درخواست‌های در انتظار می‌فرستد """
    if not product_ids:
        return 0
    from products.models import Product
    from products.signals import product_back_in_stock
    sent = 0
    ids = list(product_ids)
    for start in range(0, len(ids), CHUNK):                  # سقف پارامتر IN در MSSQL
        for product in Product.objects.filter(id__in=ids[start:start + CHUNK]):
            product_back_in_stock.send_robust(sender=Product, product=product)
            sent += 1
    return sent


def sweep_stock_alerts():
    """ هر درخواست در انتظارِ کالای قابل‌فروش اعلان می‌گیرد. خروجی: رشته‌ی کوتاه برای لاگ تسک """
    from products.models import Product, StockAlert

    pending = StockAlert.objects.filter(product=OuterRef('pk'), status=StockAlert.STATUS_PENDING)
    available_ids = []
    checked = 0
    for product in Product.objects.filter(Exists(pending)).iterator(chunk_size=CHUNK):
        checked += 1
        if available_of(product) > 0:
            available_ids.append(product.id)
    sent = send_back_in_stock(available_ids)
    if sent:
        logger.info('اطلاع موجودی: %s کالای دارای درخواست در انتظار قابل‌فروش بود و اعلان شد.', sent)
    return f'products_with_pending_alerts={checked} notified_products={sent}'
