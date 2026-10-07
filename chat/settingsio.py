from products.models import SiteSettings


def cfg():
    """ تنظیمات سایت برای چت: از کش، و اگر Redis قطع بود مستقیم از دیتابیس (چت با قطع Redis نمی‌میرد) """
    try:
        return SiteSettings.cached()
    except Exception:  # noqa: BLE001
        return SiteSettings.load()
