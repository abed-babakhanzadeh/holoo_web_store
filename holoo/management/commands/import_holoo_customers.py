import csv

from django.core.management.base import BaseCommand, CommandError

from holoo.client import HolooClient
from holoo.customers import OUTCOME_LABELS, REASON_LABELS, apply_plan, build_plan, csv_rows, fetch_customer_rows


class Command(BaseCommand):
    help = (
        'مشتریان هلو را به‌عنوان کاربر سایت وارد می‌کند (با کد هلو و سطح قیمتِ هلو؛ تأیید خودکار هنگام تکمیل پروفایل). '
        'پیش‌فرض فقط گزارش می‌دهد؛ با --apply واقعاً می‌نویسد. فقط از هلو می‌خواند؛ چیزی در هلو تغییر نمی‌کند. '
        'اجرای دوباره بی‌خطر است.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='واقعاً کاربران را بساز/وصل کن (پیش‌فرض: فقط گزارش)')
        parser.add_argument('--report', help='مسیر فایل CSV گزارش (نتیجه‌ی هر مشتری و موارد ردشده با دلیل)')

    def handle(self, *args, **opts):
        client = HolooClient()
        rows = fetch_customer_rows(client)
        if rows is None:
            raise CommandError(f'خواندن مشتریان هلو ممکن نشد ({client.last_error or "حالت mock/غیرفعال"}).')

        plan = build_plan(rows)
        self.stdout.write(f'مشتریان هلو: {len(rows)} | کاندیدای ورود: {len(plan.candidates)}')
        for reason, count in plan.reason_counts().most_common():
            self.stdout.write(f'  ردشده — {REASON_LABELS[reason]}: {count}')

        report = apply_plan(plan, apply=opts['apply'])
        for outcome, count in report.counts.most_common():
            self.stdout.write(f'  {OUTCOME_LABELS.get(outcome, outcome)}: {count}')
        for entry in report.entries:
            if entry['outcome'] in ('conflict', 'error'):
                self.stdout.write(self.style.WARNING(f"  ⚠ کد {entry['code']} {entry['name']} ({entry['mobile']}): {entry['detail']}"))

        if opts['report']:
            with open(opts['report'], 'w', encoding='utf-8-sig', newline='') as handle:
                writer = csv.writer(handle)
                writer.writerow(['کد هلو', 'نام', 'موبایل', 'نتیجه', 'توضیح'])
                writer.writerows(csv_rows(plan, report))
            self.stdout.write(f'گزارش نوشته شد: {opts["report"]}')

        if not opts['apply']:
            self.stdout.write(self.style.WARNING('این فقط گزارش بود؛ برای اجرای واقعی: --apply'))
        else:
            self.stdout.write(self.style.SUCCESS('انجام شد. ورود دوباره‌ی دستور فقط مشتریان تازه را اضافه می‌کند.'))
