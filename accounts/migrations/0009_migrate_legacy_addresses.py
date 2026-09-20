"""
Data Migration: آدرس تک‌فیلدیِ قدیمی CustomUser (state/city/postal_code/address) ← مدل Address.

- برای هر کاربری که حداقل یکی از این چهار فیلد را دارد، یک «آدرس اصلی» پیش‌فرض ساخته می‌شود
  (گیرنده = نام و موبایل خود کاربر). استان/شهر با نرمال‌سازی «ي/ی، ك/ک، نیم‌فاصله» با جدول‌های
  locations تطبیق داده می‌شوند؛ اگر استان پیدا شد ولی شهر نه، شهر تازه زیر همان استان ساخته می‌شود.
- ناحیه (zone) عمداً خالی می‌ماند و کاربر هنگام ویرایش آدرس آن را انتخاب می‌کند.
- اگر رکوردی قابل انتقال نباشد (استان پیدا نشد / شهر خالی)، مایگریشن *متوقف می‌شود* و فهرست
  رکوردها را چاپ می‌کند، چون مایگریشن بعدی (0010) این ستون‌ها را حذف می‌کند و بی‌صدا از دست
  رفتن داده قابل‌قبول نیست. با اصلاح داده (مثلاً افزودن استان/شهر از ادمین) دوباره اجرا کنید.
- ایدمپوتنت: کاربری که از قبل آدرس دارد دوباره منتقل نمی‌شود.
- برگشت (rollback): آدرس پیش‌فرض هر کاربر به چهار فیلد قدیمی کپی می‌شود.

از مدل تاریخی استفاده می‌شود (save/delete سفارشی Address اجرا نمی‌شود)؛ خود این کد قاعده‌ی «دقیقاً یک
پیش‌فرض» را رعایت می‌کند.
"""

from django.db import migrations

from locations.text import normalize_fa


def _clean(value):
    return (value or '').strip()


def forwards(apps, schema_editor):
    User = apps.get_model('accounts', 'CustomUser')
    Address = apps.get_model('accounts', 'Address')
    Province = apps.get_model('locations', 'Province')
    City = apps.get_model('locations', 'City')

    provinces = {normalize_fa(p.name): p for p in Province.objects.all()}
    counts = {'migrated': 0, 'cities_created': 0, 'no_legacy_data': 0, 'already_has_address': 0}
    skipped = []

    for user in User.objects.all().order_by('pk'):
        state, city_name = _clean(user.state), _clean(user.city)
        postal, text = _clean(user.postal_code), _clean(user.address)
        if not (state or city_name or postal or text):
            counts['no_legacy_data'] += 1
            continue
        if Address.objects.filter(user=user).exists():
            counts['already_has_address'] += 1
            continue

        legacy = {'user_id': user.pk, 'phone': user.phone_number, 'state': state, 'city': city_name,
                  'postal_code': postal, 'address': text}
        province = provinces.get(normalize_fa(state))
        if province is None:
            skipped.append({**legacy, 'reason': 'استان با هیچ استان موجود تطبیق نداد' if state else 'استان خالی است'})
            continue
        if not city_name:
            skipped.append({**legacy, 'reason': 'شهر خالی است'})
            continue

        city = next((c for c in City.objects.filter(province=province) if normalize_fa(c.name) == normalize_fa(city_name)), None)
        if city is None:
            city = City.objects.create(province=province, name=city_name)
            counts['cities_created'] += 1

        Address.objects.create(
            user=user, title='آدرس اصلی',
            receiver_first_name=_clean(user.first_name), receiver_last_name=_clean(user.last_name),
            receiver_phone=user.phone_number, city=city, zone=None,
            postal_code=postal, address=text, is_default=True,
        )
        counts['migrated'] += 1

    print(f'\n  [آدرس‌های قدیمی] منتقل‌شده: {counts["migrated"]} | بدون داده‌ی آدرس: {counts["no_legacy_data"]} | '
          f'از قبل آدرس داشت: {counts["already_has_address"]} | شهر تازه ساخته‌شده: {counts["cities_created"]} | '
          f'ردشده: {len(skipped)}')
    if skipped:
        for row in skipped:
            print(f'    ✗ {row}')
        raise RuntimeError(
            f'{len(skipped)} کاربر آدرسی دارند که قابل انتقال نیست (فهرست بالا). پیش از حذف ستون‌های قدیمی '
            f'استان/شهرِ مربوطه را از ادمین اصلاح یا اضافه کنید و دوباره migrate بزنید.'
        )


def backwards(apps, schema_editor):
    """ آدرس پیش‌فرض هر کاربر را به فیلدهای قدیمی برمی‌گرداند (پس از برگشتِ 0010 که ستون‌ها را دوباره می‌سازد) """
    User = apps.get_model('accounts', 'CustomUser')
    Address = apps.get_model('accounts', 'Address')
    restored = 0
    for address in Address.objects.filter(is_default=True).select_related('city', 'city__province', 'zone'):
        parts = [address.address]
        if address.zone_id:
            parts.insert(0, address.zone.name)
        User.objects.filter(pk=address.user_id).update(
            state=address.city.province.name, city=address.city.name,
            postal_code=address.postal_code or None, address='، '.join(p for p in parts if p) or None,
        )
        restored += 1
    print(f'\n  [بازگردانی آدرس‌ها] آدرس پیش‌فرض {restored} کاربر به فیلدهای قدیمی برگشت.')


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0008_address'),
        ('locations', '0002_seed_divisions_and_zones'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
