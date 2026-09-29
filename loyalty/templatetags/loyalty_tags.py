"""
تگ‌های نمایشیِ باشگاه مشتریان برای فروشگاه (Loyalty Phase 6B) - صفحه‌ی محصول و فاکتور چک‌اوت.

عمداً در اپ loyalty (نه products/orders) - تا منطق فرمول امتیاز در یک‌جا بماند و
products/orders هرگز مجبور به import از loyalty در سطح پایتون نشوند؛ فقط لایه‌ی تمپلیت با
{% load loyalty_tags %} این توابع را صدا می‌زند - هیچ importی در orders/*.py یا
products/*.py اضافه نشده و جهت وابستگی یک‌طرفه‌ی پروژه (loyalty -> products) دست‌نخورده می‌ماند.

هر دو تگ صرفاً *برآورد نمایشی* می‌دهند؛ منبع واقعی و نهاییِ امتیاز همیشه
loyalty/earning.py::calculate_order_earn_points در لحظه‌ی پرداخت موفق است - این فایل هرگز
چیزی در لجر نمی‌نویسد (کاملاً فقط‌خواندنی).
"""

from django import template

from products.models import SiteSettings
from products.pricing import final_price

register = template.Library()


@register.simple_tag
def product_loyalty_points(product, user):
    """
    امتیازی که با خرید *همین یک کالا* (به‌تنهایی) کسب می‌شود - فقط در حالت «مبلغ خرید»؛ در حالت
    «تعداد سفارش» امتیاز به کل سفارش تعلق می‌گیرد نه به یک قلم، پس اینجا None برمی‌گردد (نمایش
    این بج روی کارت تکی محصول در آن حالت اصلاً معنا ندارد).
    """
    settings_obj = SiteSettings.cached()
    if not settings_obj.loyalty_activated_at:
        return None
    if settings_obj.loyalty_mode != SiteSettings.LOYALTY_MODE_AMOUNT:
        return None
    price = final_price(product, user)
    if price is None:
        return None
    points = int(price // (settings_obj.loyalty_amount_step or 1))
    return points if points > 0 else None


@register.simple_tag
def order_loyalty_points_estimate(items_total):
    """
    برآورد امتیاز *کل فاکتور* بر مبنای مبلغ کل اقلام (items_total - پیش از تخفیف کد سفارش/هزینه‌ی
    ارسال، دقیقاً هم‌مبنای loyalty/earning.py::calculate_order_earn_points). در حالت «تعداد
    سفارش» عدد ثابت هر سفارش را برمی‌گرداند (مستقل از مبلغ).
    """
    settings_obj = SiteSettings.cached()
    if not settings_obj.loyalty_activated_at:
        return None
    if settings_obj.loyalty_mode == SiteSettings.LOYALTY_MODE_AMOUNT:
        points = int(items_total // (settings_obj.loyalty_amount_step or 1))
        return points if points > 0 else None
    return settings_obj.loyalty_points_per_order
