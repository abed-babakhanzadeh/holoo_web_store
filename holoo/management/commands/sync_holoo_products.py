from django.core.management.base import BaseCommand

from holoo.tasks import sync_products_from_holoo


class Command(BaseCommand):
    help = "سینک دستیِ فوری محصولات/دسته‌بندی‌ها از هلو (اجرای همزمان، بدون نیاز به Celery worker)"

    def handle(self, *args, **options):
        self.stdout.write("شروع سینک محصولات از هلو...")
        result = sync_products_from_holoo.apply()
        self.stdout.write(self.style.SUCCESS(str(result.result)))
