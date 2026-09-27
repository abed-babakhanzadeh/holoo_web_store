from django.apps import AppConfig


class PaymentsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'payments'
    verbose_name = 'پرداخت‌ها'

    def ready(self):
        # ثبت آمار این اپ در رجیستری پیشخوان (accounts.stats)
        from . import stats  # noqa: F401
        # اتصال رسیور order_canceled (Wallet Phase 4: بازگشت سهم کیف‌پول در لغو سفارش)
        from . import receivers  # noqa: F401
