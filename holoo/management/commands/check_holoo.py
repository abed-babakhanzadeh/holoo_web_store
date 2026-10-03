import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from holoo.analysis import analyze_products, compare_with_site
from holoo.client import HolooClient
from holoo.conf import get_config


class Command(BaseCommand):
    help = (
        "تشخیص فقط‌خواندنی اتصال به هلو: تنظیمات (بدون رمز)، لاگین، نسخه/واحد پول، تعداد و ساختار کالاها و مقایسه با سایت. "
        "هیچ درخواست نوشتنی به هلو نمی‌فرستد و چیزی در دیتابیس سایت نمی‌نویسد."
    )

    def add_arguments(self, parser):
        parser.add_argument('--pages', type=int, default=0, help='حداکثر تعداد صفحه (۰ = همه)')
        parser.add_argument('--page-size', type=int, default=500)
        parser.add_argument('--no-site-compare', action='store_true', help='مقایسه با ErpCodeهای دیتابیس سایت انجام نشود')
        parser.add_argument('--save-raw', help='ذخیره‌ی پاسخ خام کالاها در این مسیر (باید بیرون از پوشه‌ی پروژه/media باشد)')
        parser.add_argument('--save-report', help='ذخیره‌ی گزارش تحلیل (JSON) در این مسیر')

    def handle(self, *args, **opts):
        out = self.stdout.write
        config = get_config()
        client = HolooClient(config)

        out('== تنظیمات (رمز نمایش داده نمی‌شود) ==')
        for key, value in config.masked().items():
            out(f'  {key}: {value}')
        for warning in config.warnings:
            out(self.style.WARNING(f'  هشدار: {warning}'))
        if config.read_is_mock:
            raise CommandError('HOLOO_READ_MODE=mock است؛ تشخیص واقعی ممکن نیست.')
        problems = config.problems()
        if problems:
            raise CommandError(' '.join(problems))

        out('\n== لاگین ==')
        login = client.login()
        if login.get('status') != 'success':
            raise CommandError(f"لاگین ناموفق: {login.get('message')} (code={login.get('code')})")
        out(self.style.SUCCESS('  لاگین موفق (توکن فقط در حافظه نگه داشته می‌شود)'))

        for label, path in (('نسخه', 'Version'), ('تنظیمات/واحد پول', 'Settings')):
            out(f'\n== {label} (GET /{path}) ==')
            out(f'  {json.dumps(client.get_json(path), ensure_ascii=False)[:600]}')

        out('\n== تعداد کل کالاها (GET /Product/count) ==')
        reported = client.get_product_count()
        out(f'  {reported}')

        out('\n== واکشی صفحه‌ها ==')
        items, page = [], 1
        while not opts['pages'] or page <= opts['pages']:
            data = client.get_products(page=page, items_per_page=opts['page_size'])
            if data is None:
                raise CommandError(f'واکشی صفحه {page} ناموفق: {client.last_error}')
            batch = data.get('product', [])
            out(f'  صفحه {page}: {len(batch)} کالا')
            items.extend(batch)
            if len(batch) < opts['page_size']:
                break
            page += 1
        out(f'  مجموع واکشی‌شده: {len(items)} (گزارش هلو: {reported})')

        report = analyze_products(items)
        report['reported_count'] = reported
        if not opts['no_site_compare']:
            from products.models import Product
            report['site_compare'] = compare_with_site(
                items, Product.objects.exclude(erp_code__isnull=True).values_list('erp_code', flat=True),
            )

        out('\n== تحلیل ==')
        out(json.dumps(report, ensure_ascii=False, indent=2))

        for option, payload in (('save_raw', {'product': items}), ('save_report', report)):
            if opts[option]:
                path = Path(opts[option])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
                out(f'ذخیره شد: {path}')
