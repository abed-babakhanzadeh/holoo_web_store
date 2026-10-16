"""
پایش اتصال به وب‌سرویس هلو و هشدار به مدیر.

چرا لازم است (۱۶ اکتبر ۲۰۲۶): وب‌سرویس هلو (سرویس ویندوز TNCHolooWebService) چند روز خاموش ماند و هیچ‌کس نفهمید؛ فاکتورها بی‌صدا
«در صف» می‌ماندند و سینک موجودی/قیمت هم متوقف بود، چون خطای اتصال فقط در لاگ سرور می‌آید و تسک‌ها با فاصله‌ی افزایشی دوباره تلاش
می‌کنند. تسک check_holoo_health (هر ۵ دقیقه، Beat) هلو را می‌سنجد:

  - قطع بیش از ALERT_AFTER ← یک پیامک «هلو قطع است» به مدیر (شماره‌های مدیر تنظیمات سایت)، و هر REMIND_EVERY یک یادآوری؛
  - وصل‌شدن بعد از هشدار ← یک پیامک «وصل شد»؛
  - وضعیت قطع در لیست سفارش‌های ادمین هم نشان داده می‌شود (current_outage).

وضعیت در کش (Redis) نگه داشته می‌شود؛ اگر کش پاک شود فقط شمارش قطعی از نو شروع می‌شود، چیزی خراب نمی‌شود.
"""

import logging
from datetime import datetime, timedelta

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

DOWN_SINCE_KEY = 'holoo:health:down_since'
LAST_ALERT_KEY = 'holoo:health:last_alert_at'
STATE_TTL = 7 * 24 * 3600

ALERT_AFTER = timedelta(minutes=10)
REMIND_EVERY = timedelta(hours=3)
PROBE_TIMEOUT = 10


def _get_time(key):
    value = cache.get(key)
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _set_time(key, when):
    cache.set(key, when.isoformat(), STATE_TTL)


def current_outage():
    """ {'since': datetime, 'minutes': int} وقتی آخرین سنجش ناموفق بوده، وگرنه None (برای بنر ادمین) """
    try:
        since = _get_time(DOWN_SINCE_KEY)
    except Exception:  # noqa: BLE001 - کش در دسترس نبود: بنر نشان داده نمی‌شود
        return None
    if since is None:
        return None
    return {'since': since, 'minutes': int((timezone.now() - since).total_seconds() // 60)}


def _pending_orders_count():
    from orders.models import Order
    return Order.objects.filter(approved_at__isnull=False).filter(holoo_invoice_id__isnull=True) \
        .exclude(status__in=('canceled', 'rejected_stock')).count()


def probe(client):
    """ True اگر وب‌سرویس هلو جواب داد. /Version را می‌خواند (توکن کش‌شده هم استفاده می‌شود و ۴۰۱ یک‌بار تازه می‌شود) """
    return client.get_json('Version', timeout=PROBE_TIMEOUT) is not None


def check_holoo_health(client=None, now=None):
    """ یک سنجش؛ خروجی رشته‌ی کوتاه برای لاگ تسک: skipped / ok / down(...) / recovered(...) """
    from notifications.service import notify_admin
    from .client import HolooClient

    client = client or HolooClient()
    if client.config.read_is_mock:
        return 'skipped (holoo read mode is mock)'
    now = now or timezone.now()

    if probe(client):
        since = _get_time(DOWN_SINCE_KEY)
        alerted = _get_time(LAST_ALERT_KEY)
        cache.delete_many([DOWN_SINCE_KEY, LAST_ALERT_KEY])
        if since is not None and alerted is not None:                  # فقط اگر هشدار «قطع» رفته بود، «وصل شد» می‌رود
            minutes = max(1, int((now - since).total_seconds() // 60))
            notify_admin('holoo_recovered_admin', minutes=minutes, pending=_pending_orders_count())
            return f'recovered after {minutes} min'
        return 'ok'

    since = _get_time(DOWN_SINCE_KEY)
    if since is None:
        since = now
        _set_time(DOWN_SINCE_KEY, now)
    down_for = now - since
    if down_for < ALERT_AFTER:
        return f'down {int(down_for.total_seconds() // 60)} min (below alert threshold)'

    last_alert = _get_time(LAST_ALERT_KEY)
    if last_alert is None or now - last_alert >= REMIND_EVERY:
        minutes = int(down_for.total_seconds() // 60)
        notify_admin('holoo_down_admin', minutes=minutes, pending=_pending_orders_count())
        _set_time(LAST_ALERT_KEY, now)
        logger.warning('اتصال هلو %s دقیقه است قطع است؛ هشدار به مدیر فرستاده شد.', minutes)
        return f'down {minutes} min, alert sent'
    return f'down {int(down_for.total_seconds() // 60)} min, alert already sent'
