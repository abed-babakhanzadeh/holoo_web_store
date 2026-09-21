"""
Data Migration: مدل قدیمی products.Discount (درصدِ یک محصول در یک بازه) ← promotions.Promotion.

هر تخفیف قدیمی *عیناً* (همان درصد، بازه، فعال/غیرفعال) به یک Promotion از نوع درصدی با یک هدف «محصول» تبدیل می‌شود و
در باکس شگفت‌انگیز هم نمایش داده می‌شود (رفتار قبلی). مایگریشن بعدیِ اپ products (0021) خودِ جدول Discount را حذف می‌کند.

برگشت (rollback): هر Promotion درصدی که دقیقاً یک هدفِ «محصول» (بدون استثنا) دارد به Discount برمی‌گردد؛ بقیه‌ی
تخفیف‌ها (دسته/برند/مبلغ ثابت/...) در مدل قدیمی بیان‌پذیر نیستند و برنمی‌گردند (تعدادشان چاپ می‌شود).
"""

from django.db import migrations


def forwards(apps, schema_editor):
    Discount = apps.get_model('products', 'Discount')
    Promotion = apps.get_model('promotions', 'Promotion')
    PromotionTarget = apps.get_model('promotions', 'PromotionTarget')

    migrated = 0
    for discount in Discount.objects.select_related('product').order_by('id'):
        promotion = Promotion.objects.create(
            title=f'{discount.product.name} — {discount.percent}٪'[:200],
            kind='percent', value=discount.percent,
            starts_at=discount.starts_at, ends_at=discount.ends_at, is_active=discount.is_active,
            show_in_flash_deals=True,
        )
        PromotionTarget.objects.create(promotion=promotion, target_type='product', product=discount.product)
        migrated += 1
    print(f'\n  [تخفیف‌های قدیمی] {migrated} تخفیف به Promotion منتقل شد.')


def backwards(apps, schema_editor):
    Discount = apps.get_model('products', 'Discount')
    Promotion = apps.get_model('promotions', 'Promotion')

    restored = skipped = 0
    for promotion in Promotion.objects.prefetch_related('targets').order_by('id'):
        targets = list(promotion.targets.all())
        simple = (
            promotion.kind == 'percent' and len(targets) == 1
            and targets[0].target_type == 'product' and not targets[0].is_exclusion
        )
        if simple:
            Discount.objects.create(
                product_id=targets[0].product_id, percent=promotion.value,
                starts_at=promotion.starts_at, ends_at=promotion.ends_at, is_active=promotion.is_active,
            )
            restored += 1
        else:
            skipped += 1
    print(f'\n  [بازگردانی تخفیف‌ها] {restored} تخفیف به مدل قدیمی برگشت؛ {skipped} تخفیف در مدل قدیمی بیان‌پذیر نبود.')


class Migration(migrations.Migration):

    dependencies = [
        ('promotions', '0001_initial'),
        ('products', '0020_remove_sitesettings_shipping_cost'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
