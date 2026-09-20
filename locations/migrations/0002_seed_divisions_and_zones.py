"""
Data Migration: بارگذاری استان‌ها، شهرها و نواحی اولیه‌ی ارسال از locations/data/iran_divisions.json.

ایدمپوتنت است (اجرای دوباره چیزی را تکرار نمی‌کند). برگشت (rollback) عمداً چیزی حذف نمی‌کند: بعد از
اجرا ممکن است آدرس‌های کاربران به این رکوردها (با PROTECT) وصل شده باشند؛ پاک‌کردنشان خطرناک است.
"""

from django.db import migrations

from locations import seed


def load(apps, schema_editor):
    Province = apps.get_model('locations', 'Province')
    City = apps.get_model('locations', 'City')
    DeliveryZone = apps.get_model('locations', 'DeliveryZone')

    stats = seed.load_divisions(seed.load_data_file(), Province, City)
    zones = seed.seed_initial_zones(Province, City, DeliveryZone)
    print(f'\n  locations: {stats["provinces_created"]} استان، {stats["cities_created"]} شهر، {zones} ناحیه‌ی ارسال ساخته شد.')


class Migration(migrations.Migration):

    dependencies = [
        ('locations', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(load, migrations.RunPython.noop),
    ]
