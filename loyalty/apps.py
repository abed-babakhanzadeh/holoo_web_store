from django.apps import AppConfig


class LoyaltyConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'loyalty'
    verbose_name = 'باشگاه مشتریان'

    def ready(self):
        # اتصال ریسیور payment_succeeded (Loyalty Phase 2B: کسب امتیاز خودکار)
        from . import receivers  # noqa: F401
        # ثبت سطح وفاداری مؤثر در رجیستری accounts.stats (Loyalty Phase 5A-2)
        from . import stats  # noqa: F401
