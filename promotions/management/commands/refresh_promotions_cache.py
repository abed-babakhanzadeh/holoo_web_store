from django.core.management.base import BaseCommand

from django.utils import timezone

from promotions import index


class Command(BaseCommand):
    help = 'کش شاخص تخفیف‌ها را پاک و از دیتابیس دوباره می‌سازد (بعد از تغییر مستقیم دیتابیس یا مهاجرت)'

    def handle(self, *args, **options):
        index.invalidate()
        built = index.get_index()
        active = sum(1 for rule in built.rules if rule.in_window(timezone.now()))
        self.stdout.write(self.style.SUCCESS(
            f'شاخص ساخته شد: {len(built.rules)} تخفیف فعال‌شده ({active} در بازه‌ی زمانی فعلی) | '
            f'تخفیف‌های خودکار {"روشن" if built.policy.promotions_enabled else "خاموش"} است.'
        ))
