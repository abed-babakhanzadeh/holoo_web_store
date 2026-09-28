"""
موتور کسب امتیاز از پرداخت موفق (Loyalty Phase 2B).

فرمول امتیازِ *یک* سفارش - آینه‌ی دقیق دو فرمول زنده‌ی CustomUser.get_loyalty_points
(accounts/models.py:418-423) اما محاسبه‌شده فقط برای همین سفارش، نه تجمیعی روی کل تاریخچه‌ی
کاربر. عمداً کاملاً مستقل از accounts.models/orders.stats - این دو هرگز از اینجا صدا زده
نمی‌شوند و هرگز تغییر نمی‌کنند (مصوبه‌ی ممیزی فاز صفر: Loyalty Tier در برابر Loyalty Points
Ledger؛ نگاه کنید گزارش تحلیل اثرات فاز ۲، بخش د). نتیجه یک‌بار در لحظه‌ی کسب اسنپ‌شات و در
LoyaltyTransaction.amount ذخیره می‌شود؛ تغییر بعدی SiteSettings.loyalty_* هرگز تراکنش‌های
قبلاً ثبت‌شده را تغییر نمی‌دهد.

این ماژول هیچ importی از payments/orders.signals/returns ندارد - فقط با پارامترهایی که از
طریق سیگنال به loyalty/receivers.py می‌رسند (order, transaction) کار می‌کند؛ جهت وابستگی
همچنان یک‌طرفه (loyalty → products.SiteSettings، loyalty → orders فقط برای خوانش مدل) است.
"""

from products.models import SiteSettings

from . import services
from .models import LoyaltyTransaction


def calculate_order_earn_points(order, settings_obj):
    """
    امتیاز *همین سفارش* (نه تجمیعی روی کل تاریخچه‌ی کاربر).

    مبنا: order.items_total = Σ(OrderItem.price × OrderItem.quantity) روی همه‌ی ردیف‌های همین
    سفارش (orders/models.py:143-146 → OrderItem.get_cost @ orders/models.py:241-242) - دقیقاً
    همان فرمولی که orders/stats.py:orders_paid_net_amount برای Tier زنده (تجمیعی روی چند
    سفارش) استفاده می‌کند؛ اینجا فقط برای یک سفارش.

    دو استثنای صریح (هم‌سو با فرمول زنده‌ی فعلی accounts/models.py:421-423):
      - OrderItem.price از قبل *بعد از* تخفیف خودکار محصول (Promotion) است، اما order_discount
        (تخفیف کد سفارش سطح سفارش) هرگز از items_total کم نمی‌شود - چون order_discount مستقیم
        در هیچ‌کدام از OrderItem.price ذخیره نشده (فقط یک عدد کلی روی خودِ Order).
      - order.shipping_cost مطلقاً خوانده نمی‌شود.
    """
    if settings_obj.loyalty_mode == SiteSettings.LOYALTY_MODE_AMOUNT:
        net_amount = order.items_total
        return int(net_amount // (settings_obj.loyalty_amount_step or 1))
    return settings_obj.loyalty_points_per_order


def _is_eligible(order, payment_transaction, settings_obj):
    if payment_transaction.status != 'success':
        return False
    if order is None or order.user_id is None:
        return False
    if order.status == 'canceled':
        return False
    if not settings_obj.loyalty_activated_at:
        return False
    if order.created_at < settings_obj.loyalty_activated_at:
        return False
    return True


def earn_from_payment(order, payment_transaction):
    """
    نقطه‌ی ورود واحد از loyalty/receivers.py::on_payment_succeeded.

    order را همین‌جا refresh_from_db می‌کنیم چون سیگنال payment_succeeded بعد از commit
    (transaction.on_commit) شلیک می‌شود؛ status باید *لحظه‌ی پردازش این شنونده* را نشان دهد
    (نه لحظه‌ی قفل‌گیری Transaction در PaymentCallbackView) - مثلاً اگر بین آن دو لحظه سفارش
    مستقل canceled شده باشد. این فقط یک گیت بهترین‌کوشش (best-effort) است؛ ایمنی واقعی در برابر
    رقابت هم‌زمان از idempotency_key + قفل ردیفی خودِ credit_points می‌آید (loyalty/services.py)
    نه از این چک.

    Silent No-Op اگر هرکدام از شرایط احراز برقرار نباشد یا امتیاز محاسبه‌شده صفر/منفی شود -
    هیچ‌کدام از این حالت‌ها خطا نیستند؛ این تابع در آن حالت‌ها فقط None برمی‌گرداند، هیچ
    استثنایی پرتاب نمی‌کند و هیچ رکوردی نمی‌سازد.
    """
    order.refresh_from_db()
    settings_obj = SiteSettings.cached()

    if not _is_eligible(order, payment_transaction, settings_obj):
        return None

    points = calculate_order_earn_points(order, settings_obj)
    if points <= 0:
        return None

    return services.credit_points(
        order.user, points, LoyaltyTransaction.EARN_ORDER,
        f'کسب امتیاز از پرداخت موفق سفارش #{order.id}',
        source_type='order', source_id=order.id,
        idempotency_key=f'loyalty-earn-order-{order.id}',
    )
