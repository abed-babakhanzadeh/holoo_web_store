from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'notifications'
    verbose_name = 'اطلاع‌رسانی'

    def ready(self):
        from django.db.models.signals import post_migrate

        from . import receivers  # noqa: F401

        post_migrate.connect(_sync_settings_after_migrate, sender=self, dispatch_uid='notifications_sync_settings')


def _sync_settings_after_migrate(sender, using=None, **kwargs):
    """ بعد از هر migrate، قالب‌های تازه‌ی کد ردیف تنظیمات می‌گیرند (بدون نیاز به data-migration جدا برای هر قالب) """
    from .models import sync_notification_settings
    try:
        sync_notification_settings()
    except Exception:  # noqa: BLE001 - جدول هنوز نیست (مثلاً migrate جزئی)؛ بعداً همگام می‌شود
        pass
