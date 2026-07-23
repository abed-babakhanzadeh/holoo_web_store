import re
from pathlib import Path

from django.conf import settings

from .models import Product, ProductImage

CATALOG_DIR_NAME = 'products/catalog'
FILENAME_RE = re.compile(r'^(?P<code>.+)-(?P<idx>\d{1,3})\.(?P<ext>jpe?g|png)$', re.IGNORECASE)
MAX_GALLERY_INDEX = 50


def _catalog_dir() -> Path:
    path = Path(settings.MEDIA_ROOT) / CATALOG_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def sync_product_images():
    """
    پوشه‌ی media/products/catalog/ را می‌خواند و فایل‌هایی با نام «{erp_code}-{شماره}.jpg/png»
    را به تصویر اصلی محصول (شماره ۱) یا گالری آن (شماره ۲ به بعد) وصل می‌کند.

    Idempotent است: اجرای دوباره روی وضعیت بدون‌تغییر کاری انجام نمی‌دهد. عکاس/گرافیست فقط کافیست
    فایل‌ها را با این قرارداد در پوشه کپی کند؛ حذف کردن یک فایل از پوشه هم رکورد متناظرش را پاک می‌کند.
    """
    catalog_dir = _catalog_dir()

    matched = 0
    updated = 0
    unmatched = []
    seen_paths = set()

    groups = {}
    for entry in sorted(catalog_dir.iterdir()):
        if not entry.is_file():
            continue
        m = FILENAME_RE.match(entry.name)
        if not m:
            unmatched.append(entry.name)
            continue
        idx = int(m.group('idx'))
        if idx < 1 or idx > MAX_GALLERY_INDEX:
            unmatched.append(entry.name)
            continue
        groups.setdefault(m.group('code'), {})[idx] = entry.name

    for code, files_by_idx in groups.items():
        product = Product.objects.filter(erp_code=code).first()
        if not product:
            unmatched.extend(files_by_idx.values())
            continue

        for idx, filename in sorted(files_by_idx.items()):
            rel_path = f'{CATALOG_DIR_NAME}/{filename}'
            seen_paths.add(rel_path)
            matched += 1

            if idx == 1:
                if (product.main_image.name or '') != rel_path:
                    product.main_image = rel_path
                    product.save(update_fields=['main_image'])
                    updated += 1
            else:
                image_obj, created = ProductImage.objects.get_or_create(
                    product=product, order=idx, defaults={'image': rel_path},
                )
                if created:
                    updated += 1
                elif image_obj.image.name != rel_path:
                    image_obj.image = rel_path
                    image_obj.save(update_fields=['image'])
                    updated += 1

    pruned = _prune_missing(seen_paths)

    return {
        'matched': matched,
        'updated': updated,
        'pruned': pruned,
        'unmatched': unmatched,
    }


def _prune_missing(seen_paths):
    """ ردیف‌هایی که به فایل‌های دیگر موجود در پوشه‌ی کاتالوگ اشاره می‌کنند را پاک/خالی می‌کند """
    pruned = 0

    for image in ProductImage.objects.filter(image__startswith=f'{CATALOG_DIR_NAME}/'):
        if image.image.name not in seen_paths:
            image.delete()
            pruned += 1

    for product in Product.objects.filter(main_image__startswith=f'{CATALOG_DIR_NAME}/'):
        if product.main_image.name not in seen_paths:
            product.main_image = None
            product.save(update_fields=['main_image'])
            pruned += 1

    return pruned
