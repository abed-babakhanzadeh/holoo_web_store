from django.apps import AppConfig


class HolooConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'holoo'
    verbose_name = 'یکپارچه‌سازی هلو'

    def ready(self):
        # ثبت شنونده‌های رویدادهای دامنه (سفارش/پرداخت/پروفایل).
        # کل اتصال هلو به پروژه از همین یک خط عبور می‌کند.
        from . import receivers  # noqa: F401

        # تأمین‌کننده‌ی «معین حساب» مشتری برای accounts (accounts هلو را نمی‌شناسد؛ نگاه کنید accounts/ledger.py)
        from accounts.ledger import register_provider
        from .ledger import fetch_customer_ledger
        register_provider(fetch_customer_ledger)

        # تازه‌سازی موجودی کالاهای سبد لحظه‌ی خرید برای products/stock_refresh.py
        from products.stock_refresh import register_refresher
        from .stock_refresh import refresh_products
        register_refresher(refresh_products)
