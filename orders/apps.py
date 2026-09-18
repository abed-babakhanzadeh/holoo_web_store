from django.apps import AppConfig


class OrdersConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'orders'
    verbose_name = 'سفارشات'

    def ready(self):
        # ثبت آمار این اپ در رجیستری پیشخوان (accounts.stats)
        from . import stats  # noqa: F401
        # باطل‌کردن کش صفحه اصلی (پرفروش‌ترین‌ها) با تغییر سفارش
        from . import home_cache_hooks  # noqa: F401
