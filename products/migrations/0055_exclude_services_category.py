from django.db import migrations

# نام دسته‌ای که کارفرما خواسته کلاً از سایت حذف شود (۱۳ اکتبر ۲۰۲۶). مقایسه با یکدست‌سازی ي/ی و ك/ک (هلو حروف عربی دارد).
EXCLUDED_NAMES = {'خدمات'}


def _normal(text):
    return (text or '').replace('ي', 'ی').replace('ك', 'ک').strip()


def exclude_services_category(apps, schema_editor):
    """
    دسته‌ی «خدمات» (و همه‌ی زیردسته‌هایش) را علامت «حذف کامل از سایت» می‌زند و غیرفعال می‌کند. کالاهای آن در سینک بعدی پاک
    می‌شوند و سینک هلو دوباره واردشان نمی‌کند. اگر چنین دسته‌ای نباشد کاری نمی‌کند. برگشت‌پذیر: از ادمین دسته تیک را بردارید.
    """
    Category = apps.get_model('products', 'Category')
    roots = [c.pk for c in Category.objects.all().only('pk', 'name') if _normal(c.name) in EXCLUDED_NAMES]
    if not roots:
        return
    ids = set(roots)
    frontier = list(roots)
    while frontier:
        children = list(Category.objects.filter(parent_id__in=frontier).values_list('pk', flat=True))
        children = [pk for pk in children if pk not in ids]
        ids.update(children)
        frontier = children
    Category.objects.filter(pk__in=ids).update(exclude_from_site=True, is_active=False)
    # کش ۲۴ساعته‌ی «دسته‌های پرفروش» صفحه‌ی اصلی و منوی دسته‌بندی‌ها (update() سیگنال نمی‌فرستد؛ بدون این، دسته تا یک روز دیده می‌شد)
    try:
        from django.core.cache import cache
        cache.delete_many(['home:top_category_ids', 'storefront:nav_categories:v2', 'storefront:site_settings'])
    except Exception:  # noqa: BLE001 - کش در دسترس نباشد، TTL و بطلان رویدادی کار را تمام می‌کنند
        pass


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0054_category_exclude_from_site'),
    ]

    operations = [
        migrations.RunPython(exclude_services_category, migrations.RunPython.noop),
    ]
