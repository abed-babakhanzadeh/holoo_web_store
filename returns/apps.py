from django.apps import AppConfig


class ReturnsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'returns'
    verbose_name = 'مرجوعی کالا'

    def ready(self):
        # ثبت تأمین‌کننده‌ی «خریدار مرجوع‌کرده» در رجیستری reviews.purchases
        from . import purchases  # noqa: F401
