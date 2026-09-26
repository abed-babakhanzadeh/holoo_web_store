"""ثبت تنظیمات وفاداری در رجیستری accounts.stats، تا accounts مستقیم SiteSettings را import نکند."""

from accounts.stats import register_config

from .models import SiteSettings


@register_config('loyalty_settings')
def loyalty_settings():
    """
    تنظیمات فعلیِ امتیاز/سطح وفاداری، برای مصرف در accounts.models.CustomUser.
    از SiteSettings.cached() می‌خواند (همان کش ۱۵-دقیقه‌ای که با ذخیره در ادمین باطل می‌شود).
    """
    s = SiteSettings.cached()
    return {
        'mode': s.loyalty_mode,
        'points_per_order': s.loyalty_points_per_order,
        'amount_step': s.loyalty_amount_step or 1,
        'thresholds': (
            0,
            s.loyalty_threshold_bronze,
            s.loyalty_threshold_silver,
            s.loyalty_threshold_gold,
            s.loyalty_threshold_diamond,
        ),
    }
