from django.apps import AppConfig


class HolooConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'holoo'
    verbose_name = 'یکپارچه‌سازی هلو'

    def ready(self):
        # ثبت شنونده‌های رویدادهای دامنه (سفارش/پرداخت/پروفایل).
        # کل اتصال هلو به پروژه از همین یک خط عبور می‌کند.
        from . import receivers  # noqa: F401
