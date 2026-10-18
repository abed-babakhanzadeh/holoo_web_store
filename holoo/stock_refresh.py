"""
تأمین‌کننده‌ی «تازه‌سازی موجودی هنگام خرید» برای products/stock_refresh.py.

برای هر کالای سبد، موجودی را مستقیم از هلو می‌پرسد (GET /Product?code=<کد کالا>، حدود نیم‌ثانیه) و با همان نویسنده‌ی واحد سینک
(apply_holoo_product_state) در دیتابیس می‌نشاند؛ پس رزرو و کنترل موجودیِ بعدیِ سفارش با عدد تازه کار می‌کند. درخواست‌ها هم‌زمان
(چند رشته، فقط HTTP) و با سقف زمانی کل ارسال می‌شوند؛ نوشتن در دیتابیس فقط در رشته‌ی اصلی انجام می‌شود. کالای بی‌کد، خطای شبکه یا
پاسخ ناشناخته رد می‌شود (آخرین موجودی سینک می‌ماند).
"""

import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout

from django.utils import timezone

from products.stock import available_of

from .client import HolooClient
from .product_state import apply_holoo_product_state

logger = logging.getLogger(__name__)

MAX_WORKERS = 6
PER_REQUEST_TIMEOUT = 6
TOTAL_BUDGET = 9          # ثانیه؛ بیشتر از این منتظر هلو نمی‌مانیم
# اجرای تست‌ها هرگز نباید به هلو واقعی وصل شود (تنظیمات تست هم HOLOO_READ_MODE=real می‌ماند)؛ تست‌های خودِ این ماژول آن را False می‌کنند
TEST_RUN = 'test' in sys.argv[1:2]


def _fetch(client, product):
    data = client.get_json('Product', params={'code': product.product_code}, timeout=PER_REQUEST_TIMEOUT)
    rows = data.get('product') if isinstance(data, dict) else None
    return next((row for row in rows or [] if row.get('ErpCode') == product.erp_code), None) if data is not None else False


def refresh_products(products):
    from .stock_alerts import send_back_in_stock
    from .tasks import classify_item

    if TEST_RUN:
        return {'refreshed': 0, 'failed': 0, 'skipped': 'test run'}
    client = HolooClient()
    if client.config.read_is_mock:
        return {'refreshed': 0, 'failed': 0, 'skipped': 'mock'}
    unique = {}
    for product in products:
        if product.product_code and product.pk not in unique:
            unique[product.pk] = product
    if not unique:
        return {'refreshed': 0, 'failed': 0}
    try:
        client._get_auth_header()                          # یک لاگین در رشته‌ی اصلی؛ رشته‌ها از توکن کش‌شده استفاده می‌کنند
    except Exception:  # noqa: BLE001
        return {'refreshed': 0, 'failed': len(unique)}

    observed_at = timezone.now()                           # لحظه‌ی شروع واکشی (مبنای نوشتنِ امن؛ product_state.py)
    refreshed = failed = 0
    back_in_stock = []
    pool = ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(unique)))
    futures = {pool.submit(_fetch, client, product): product for product in unique.values()}
    try:
        for future in as_completed(futures, timeout=TOTAL_BUDGET):
            product = futures[future]
            try:
                item = future.result()
            except Exception:  # noqa: BLE001
                failed += 1
                continue
            if item is False or item is None or classify_item(item) != 'ok':
                failed += 1
                continue
            was_unavailable = available_of(product) <= 0
            if apply_holoo_product_state(product, item, observed_at, source='checkout'):
                refreshed += 1
                if was_unavailable and available_of(product) > 0:
                    back_in_stock.append(product.id)
    except FuturesTimeout:
        failed += sum(1 for future in futures if not future.done())
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    send_back_in_stock(back_in_stock)
    if failed:
        logger.warning('تازه‌سازی موجودی هنگام خرید: %s کالا از هلو خوانده نشد (موجودی سینک می‌ماند).', failed)
    return {'refreshed': refreshed, 'failed': failed}
