from django.apps import AppConfig


class RecentlyViewedConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'recently_viewed'
    verbose_name = 'بازدیدهای اخیر'

    def ready(self):
        # ثبت آمار این اپ در رجیستری پیشخوان (accounts.stats)
        from . import stats  # noqa: F401
