"""
اسلایدر اصلی صفحه‌ی نخست قبلاً کاملاً هاردکد (۳ تصویر ثابت در تمپلیت) بود؛ حالا از HeroSlide
می‌آید. بدون این مایگریشن، دیتابیس تازه صفحه‌ی اصلی بدون هیچ اسلایدی بالا می‌آمد - همان درسی که
از «بنرهای صفحه اصلی» گرفتیم (نگاه کنید seed_home_banners): داده‌ی اولیه باید در مایگریشن باشد،
نه فقط در ذهن توسعه‌دهنده. همان ۳ عکسی که قبلاً هاردکد بودند این‌جا به‌عنوان اسلاید واقعی کپی و
ثبت می‌شوند تا ظاهر فعلی سایت بعد از این تغییر دست‌نخورده بماند؛ ادمین بعداً از پنل می‌تواند
جایگزین‌شان کند.
"""
import shutil
from pathlib import Path

from django.conf import settings
from django.db import migrations

SOURCE_FILENAMES = ('slider-2-1.jpg', 'slider-2-2.jpg', 'slider-2-3.jpg')


def seed_default_slides(apps, schema_editor):
    HeroSlide = apps.get_model('products', 'HeroSlide')
    if HeroSlide.objects.exists():
        return

    source_dir = Path(settings.BASE_DIR) / 'static' / 'theme' / 'assets' / 'images' / 'slider'
    dest_dir = Path(settings.MEDIA_ROOT) / 'home' / 'slider'
    dest_dir.mkdir(parents=True, exist_ok=True)

    for order, filename in enumerate(SOURCE_FILENAMES):
        source = source_dir / filename
        if not source.exists():
            continue
        relative_name = f'home/slider/{filename}'
        dest_path = Path(settings.MEDIA_ROOT) / relative_name
        if not dest_path.exists():
            shutil.copy(source, dest_path)
        HeroSlide.objects.create(image=relative_name, order=order, is_active=True)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0024_sitesettings_favicon_image_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_default_slides, noop_reverse),
    ]
