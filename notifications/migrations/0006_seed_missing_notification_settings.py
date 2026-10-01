"""
ردیف NotificationSetting برای کلیدهای تازه‌ی templates_registry.TEMPLATES که بعد از مایگریشن 0005 اضافه شدند
(مثل contact_message_admin و loyalty_tier_upgraded_customer) و هنوز ردیف ندارند می‌سازد تا در پنل ادمین
(روشن/خاموش و متن جای‌گزین) دیده شوند. ردیف‌های موجود (و تنظیمات ادمین روی آن‌ها) دست‌نخورده می‌مانند.
"""
from django.db import migrations


def seed_missing_notification_settings(apps, schema_editor):
    from notifications.templates_registry import TEMPLATES

    NotificationSetting = apps.get_model('notifications', 'NotificationSetting')
    existing = set(NotificationSetting.objects.values_list('template_key', flat=True))
    NotificationSetting.objects.bulk_create([
        NotificationSetting(template_key=key) for key in TEMPLATES if key not in existing
    ])


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0005_seed_notification_settings'),
    ]

    operations = [
        migrations.RunPython(seed_missing_notification_settings, migrations.RunPython.noop),
    ]
