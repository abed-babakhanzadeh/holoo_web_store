from django.apps import AppConfig


class ProductsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'products'
    verbose_name = 'محصولات'

    def ready(self):
        # باطل‌کردن کش منوی دسته‌بندی/تنظیمات سایت پس از تغییر در ادمین
        from . import signals  # noqa: F401
