from django.core.management.base import BaseCommand

from promotions import coupons


class Command(BaseCommand):
    help = 'رزروهای پرداخت‌نشده‌ی کد تخفیف که مهلتشان گذشته را آزاد می‌کند (اگر celery beat اجرا نمی‌شود، با cron صدا بزنید)'

    def handle(self, *args, **options):
        count = coupons.release_expired()
        self.stdout.write(self.style.SUCCESS(f'{count} رزرو منقضی آزاد شد.'))
