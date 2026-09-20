from django.core.management.base import BaseCommand

from locations import seed
from locations.models import City, DeliveryZone, Province


class Command(BaseCommand):
    help = 'بارگذاری (ایدمپوتنت) استان‌ها، شهرها و نواحی اولیه‌ی ارسال از فایل JSON'

    def add_arguments(self, parser):
        parser.add_argument('--file', default=str(seed.DEFAULT_DATA_PATH), help='مسیر فایل JSON داده')
        parser.add_argument('--skip-zones', action='store_true', help='نواحی اولیه‌ی ارسال ساخته نشوند')

    def handle(self, *args, **options):
        stats = seed.load_divisions(seed.load_data_file(options['file']), Province, City)
        self.stdout.write(f'استان جدید: {stats["provinces_created"]} | شهر جدید: {stats["cities_created"]} | '
                          f'شهر از قبل موجود: {stats["cities_existing"]} | ردیف خالی ردشده: {stats["skipped_blank"]}')
        if not options['skip_zones']:
            zones = seed.seed_initial_zones(Province, City, DeliveryZone)
            self.stdout.write(f'ناحیه‌ی جدید: {zones}')
