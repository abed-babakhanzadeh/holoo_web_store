from django.db import migrations


class Migration(migrations.Migration):
    """
    ستون additional_description با کولیشن Persian_100_CI_AS_SC ساخته شده بود.
    SQL Server هنگام ارسال مقادیر بلند nvarchar(max) با کولیشن‌های دارای پرچم _SC
    (پشتیبانی از کاراکترهای مکمل یونیکد) از مسیر داده‌محور قدیمی (سازگار با
    text/ntext) استفاده می‌کند که این کولیشن‌ها را پشتیبانی نمی‌کند و خطای
    «Cannot convert to text/ntext or collate to ...» می‌دهد. راه‌حل، تغییر
    کولیشن این ستون به همان کولیشنی است که فیلد description از قبل با آن
    مشکلی ندارد (Persian_100_CI_AS، بدون _SC).
    """

    dependencies = [
        ('products', '0005_brand_product_additional_description_and_more'),
    ]

    operations = [
        migrations.RunSQL(
            sql=(
                "ALTER TABLE products_product ALTER COLUMN additional_description "
                "NVARCHAR(MAX) COLLATE Persian_100_CI_AS NOT NULL"
            ),
            reverse_sql=(
                "ALTER TABLE products_product ALTER COLUMN additional_description "
                "NVARCHAR(MAX) COLLATE Persian_100_CI_AS_SC NOT NULL"
            ),
        ),
    ]
