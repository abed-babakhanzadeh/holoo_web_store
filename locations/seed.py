"""
بارگذاری داده‌ی اولیه‌ی استان/شهر و نواحی ارسال.

هم Data Migration (با مدل‌های تاریخی) و هم مدیریت‌کامندِ load_iran_divisions از همین توابع استفاده
می‌کنند؛ برای همین مدل‌ها به‌عنوان آرگومان گرفته می‌شوند. همه‌چیز ایدمپوتنت است: اجرای دوباره فقط
رکوردهای ناموجود را اضافه می‌کند و چیزی را که ادمین ویرایش کرده (نام، کرایه، فعال بودن) دست نمی‌زند.

قالب فایل داده (locations/data/iran_divisions.json):
    {"source": {...}, "provinces": [{"name": "قم", "cities": ["قم", "کهک", ...]}, ...]}
"""

import json
from pathlib import Path

from .text import normalize_fa

DEFAULT_DATA_PATH = Path(__file__).resolve().parent / 'data' / 'iran_divisions.json'

# نواحی اولیه‌ی ارسال با پیک. کلید: (نام استان، نام شهر). کرایه‌ی همه ۰ = «تعرفه تنظیم‌نشده» است و
# باید ادمین از پنل مقدار بگذارد.
INITIAL_ZONES = {
    ('قم', 'قم'): (
        'نیروگاه',
        'پردیسان',
        'کهک',
        'سالاریه / زنبیل‌آباد',
        'بنیاد / صفاشهر',
        'باجک / عماریاسر',
        'سایر نواحی قم',
    ),
}


def load_data_file(path=DEFAULT_DATA_PATH):
    with open(path, encoding='utf-8') as fh:
        return json.load(fh)


def load_divisions(data, province_model, city_model):
    """
    استان‌ها و شهرهای داده را اضافه می‌کند (نبودها را می‌سازد).
    خروجی: دیکشنری شمارش‌ها، برای گزارش.
    """
    stats = {'provinces_created': 0, 'cities_created': 0, 'cities_existing': 0, 'skipped_blank': 0}

    provinces = {normalize_fa(p.name): p for p in province_model.objects.all()}
    for order, entry in enumerate(data.get('provinces', []), start=1):
        name = (entry.get('name') or '').strip()
        if not name:
            stats['skipped_blank'] += 1
            continue
        province = provinces.get(normalize_fa(name))
        if province is None:
            province = province_model.objects.create(name=name, sort_order=order)
            provinces[normalize_fa(name)] = province
            stats['provinces_created'] += 1

        existing = {normalize_fa(n) for n in city_model.objects.filter(province=province).values_list('name', flat=True)}
        new_cities = []
        for city_name in entry.get('cities', []):
            city_name = (city_name or '').strip()
            key = normalize_fa(city_name)
            if not key:
                stats['skipped_blank'] += 1
                continue
            if key in existing:
                stats['cities_existing'] += 1
                continue
            existing.add(key)
            new_cities.append(city_model(province=province, name=city_name))
        city_model.objects.bulk_create(new_cities)
        stats['cities_created'] += len(new_cities)
    return stats


def seed_initial_zones(province_model, city_model, zone_model, zones=INITIAL_ZONES):
    """
    نواحی اولیه را برای شهرهای موجود می‌سازد؛ اگر شهری در دیتابیس نبود (مثلاً داده‌ی استان/شهر
    هنوز بارگذاری نشده) آن شهر بی‌صدا رد می‌شود. خروجی: تعداد ناحیه‌ی جدید.
    """
    created = 0
    for (province_name, city_name), zone_names in zones.items():
        city = next(
            (c for c in city_model.objects.filter(name__in=_spellings(city_name)).select_related('province')
             if normalize_fa(c.province.name) == normalize_fa(province_name)
             and normalize_fa(c.name) == normalize_fa(city_name)),
            None,
        )
        if city is None:
            continue
        existing = {normalize_fa(n) for n in zone_model.objects.filter(city=city).values_list('name', flat=True)}
        for order, zone_name in enumerate(zone_names, start=1):
            if normalize_fa(zone_name) in existing:
                continue
            zone_model.objects.create(city=city, name=zone_name, sort_order=order)
            created += 1
    return created


def _spellings(name):
    """ نام با هر دو املای «ی/ک» فارسی و «ي/ك» عربی، تا فیلتر دیتابیس هر دو شکل را پیدا کند """
    return {name, name.replace('ی', 'ي').replace('ک', 'ك')}
