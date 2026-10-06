"""
یک ردیف NotificationSetting به ازای هر کلید موجود در templates_registry.TEMPLATES می‌سازد
(پیش‌فرض: فعال، بدون متن جای‌گزین) تا بدون این مایگریشن، پنل ادمین برای کلیدهای موجود چیزی
برای نشان دادن نداشته باشد - همان درسی که از seed_home_banners / seed_default_hero_slides
گرفتیم: داده‌ی اولیه باید در مایگریشن باشد.

کلیدهای تازه‌ای که بعداً به TEMPLATES اضافه می‌شوند، تا اجرای مایگریشن بعدی seed نمی‌شوند؛ اما
notify() برای کلید بدون NotificationSetting رفتار قبلی (فعال، بدون override) را حفظ می‌کند
(نگاه کنید notifications/service.py) پس چیزی نمی‌شکند.
"""
from django.db import migrations


def seed_notification_settings(apps, schema_editor):
    from notifications.templates_registry import TEMPLATES

    NotificationSetting = apps.get_model('notifications', 'NotificationSetting')
    existing = set(NotificationSetting.objects.values_list('template_key', flat=True))
    NotificationSetting.objects.bulk_create([
        NotificationSetting(template_key=key, is_enabled=getattr(TEMPLATES[key], 'default_enabled', True))
        for key in TEMPLATES if key not in existing
    ])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0004_notificationsetting'),
    ]

    operations = [
        migrations.RunPython(seed_notification_settings, noop_reverse),
    ]
