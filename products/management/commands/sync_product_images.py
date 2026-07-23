from django.core.management.base import BaseCommand

from products.services import sync_product_images


class Command(BaseCommand):
    help = "همگام‌سازی تصاویر محصولات از پوشه‌ی media/products/catalog/ بر اساس نام فایل «erp_code-شماره.jpg»"

    def handle(self, *args, **options):
        self.stdout.write("شروع همگام‌سازی تصاویر محصولات...")
        result = sync_product_images()

        self.stdout.write(self.style.SUCCESS(
            f"تطبیق‌یافته: {result['matched']} | به‌روزشده: {result['updated']} | حذف‌شده (فایل دیگر نبود): {result['pruned']}"
        ))

        if result['unmatched']:
            self.stdout.write(self.style.WARNING(
                f"{len(result['unmatched'])} فایل به هیچ محصولی متصل نشد (کد کالا پیدا نشد یا نام نامعتبر بود):"
            ))
            for filename in result['unmatched']:
                self.stdout.write(f"  - {filename}")
