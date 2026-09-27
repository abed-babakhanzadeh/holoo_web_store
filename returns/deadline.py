"""
محاسبه‌ی مهلتِ مجاز برای ثبت درخواست مرجوعی کالا (Phase 1 - Part B.1).

خالص و بدون نوشتن در دیتابیس؛ فقط از SiteSettings می‌خواند (کش‌شده، نگاه کنید
products/models.py:SiteSettings.cached) و timezone.now() سرور - هرگز از کلاینت/فرم نمی‌آید،
هم‌الگوی cart/pricing.py:CartPricing.now.
"""

from datetime import timedelta

from django.utils import timezone

from products.models import SiteSettings

FRIDAY = 4   # Python: دوشنبه=۰ ... یکشنبه=۶؛ جمعه=۴


def calculate_return_deadline(delivered_at, days, unit='working_days'):
    """
    آخرین لحظه‌ی مجاز برای ثبت درخواست مرجوعی این سفارش.

      calendar_days: دقیقاً delivered_at + days روز تقویمی (days×۲۴ ساعت) - بدون تغییر ساعت.
      working_days: شمارش از فردای روز تحویل شروع می‌شود؛ هر روزی که جمعه نباشد یک روز کاری
        حساب می‌شود؛ جمعه‌ها اصلاً شمرده نمی‌شوند (نه از مهلت کم می‌شوند، نه صرف‌نظر - مهلت به
        همان اندازه که به جمعه برخورد شود جلوتر می‌رود). مهلت تا *پایان* همان روز کاریِ آخر
        معتبر است (۲۳:۵۹:۵۹.۹۹۹۹۹۹)، نه فقط تا همان ساعتِ روزِ تحویل.
    """
    if unit == SiteSettings.RETURN_PERIOD_UNIT_CALENDAR_DAYS:
        return delivered_at + timedelta(days=days)
    if unit != SiteSettings.RETURN_PERIOD_UNIT_WORKING_DAYS:
        raise ValueError(f"واحد مهلت مرجوعی ناشناخته: {unit!r}")

    cursor = delivered_at
    counted = 0
    while counted < days:
        cursor += timedelta(days=1)
        if cursor.weekday() != FRIDAY:
            counted += 1
    return cursor.replace(hour=23, minute=59, second=59, microsecond=999999)


def is_order_within_return_window(order, *, now=None):
    """
    (bool, str) - آیا *همین الان* هنوز می‌شود برای این سفارش درخواست مرجوعی ثبت کرد؟

    برای چک سریع/نمایشی (مثلاً «آیا دکمه‌ی مرجوعی نشان داده شود» در Part C) مناسب است.
    returns.services.create_return_request همین قید را مستقل و با خطای دامنه‌ای مجزا (نه فقط
    یک رشته‌ی متنی) اعمال می‌کند تا فراخوان‌کننده‌ی سرویس بتواند حالت‌های مختلف را از هم تشخیص دهد.
    """
    if order.status != 'delivered' or not order.delivered_at:
        return False, 'این سفارش هنوز تحویل داده نشده است.'

    now = now or timezone.now()
    settings_obj = SiteSettings.cached()
    deadline = calculate_return_deadline(order.delivered_at, settings_obj.return_period_days, settings_obj.return_period_unit)
    if now > deadline:
        return False, f'مهلت مرجوعی این سفارش در تاریخ {deadline:%Y-%m-%d %H:%M} به پایان رسیده است.'
    return True, ''
