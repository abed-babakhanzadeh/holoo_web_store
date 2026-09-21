from django.apps import AppConfig


class PromotionsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'promotions'
    verbose_name = 'تخفیف‌ها'

    def ready(self):
        # وابستگی معکوس ساخته نمی‌شود: products هیچ‌چیز از promotions import نمی‌کند؛ اینجا خودمان را در دو رجیستری
        # اپ products ثبت می‌کنیم (قیمت‌گذاری واحد و باکس شگفت‌انگیز)
        from products.deals import register_flash_deals_provider
        from products.pricing import register_promotion_resolver

        from . import signals  # noqa: F401
        from .flash import flash_deals_filter
        from .resolver import resolve_unit_price

        register_promotion_resolver(resolve_unit_price)
        register_flash_deals_provider(flash_deals_filter)
