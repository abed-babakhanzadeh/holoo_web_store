from django.core.management.base import BaseCommand

from products.models import HomeBanner


class Command(BaseCommand):
    help = (
        "ساخت ۴ ردیف ثابت بنر صفحه اصلی (اگر از قبل نباشند) - بدون تصویر و غیرفعال، صرفاً برای "
        "این‌که در پنل ادمین «بنرهای صفحه اصلی» قابل ویرایش شوند. روی سرور اصلی این ۴ ردیف دستی "
        "ساخته شده بودند (نه با مایگریشن)؛ روی هر دیتابیس تازه (مثل انتقال به سرور جدید) باید "
        "یک‌بار همین دستور اجرا شود."
    )

    def handle(self, *args, **options):
        created = []
        for slot, label in HomeBanner.SLOT_CHOICES:
            _, was_created = HomeBanner.objects.get_or_create(slot=slot, defaults={'is_active': False})
            if was_created:
                created.append(label)

        if created:
            self.stdout.write(self.style.SUCCESS(f"{len(created)} جایگاه بنر ساخته شد:"))
            for label in created:
                self.stdout.write(f"  - {label}")
            self.stdout.write("حالا از پنل ادمین → بنرهای صفحه اصلی، برای هرکدام تصویر آپلود و فعال کنید.")
        else:
            self.stdout.write(self.style.WARNING("هر ۴ جایگاه از قبل موجود بودند؛ کاری انجام نشد."))
