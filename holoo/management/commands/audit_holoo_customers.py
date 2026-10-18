from django.core.management.base import BaseCommand, CommandError

from holoo.client import HolooClient
from holoo.customers import fetch_customer_rows, find_mislinked_users, unlink_users

REASONS = {'mobile_differs': 'موبایل مشتری در هلو با موبایل کاربر فرق دارد', 'missing': 'مشتری با این کد در هلو پیدا نشد'}


class Command(BaseCommand):
    help = (
        'کاربرانی را پیدا می‌کند که کد هلوشان به مشتریِ دیگری (با موبایل متفاوت) اشاره می‌کند، مثلاً به‌خاطر کدملی تکراری/ساختگی. '
        'پیش‌فرض فقط گزارش می‌دهد (فقط از هلو می‌خواند)؛ با --fix کد هلوی آن کاربران پاک و همگام‌سازی مجدد (ساخت مشتریِ درست) به صف می‌رود. '
        'فاکتور و مرجوعیِ قبلاً ثبت‌شده در هلو خودکار اصلاح نمی‌شود؛ فهرستش چاپ می‌شود تا حسابدار در هلو مشتری را عوض کند.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--fix', action='store_true', help='کد هلوی کاربران ناهم‌خوان را پاک کن و همگام‌سازی را به صف بفرست')

    def handle(self, *args, **opts):
        from orders.models import Order
        from returns.models import ReturnRequest

        client = HolooClient()
        rows = fetch_customer_rows(client)
        if rows is None:
            raise CommandError(f'خواندن مشتریان هلو ممکن نشد ({client.last_error or "حالت mock/غیرفعال"}).')

        found = find_mislinked_users(rows)
        self.stdout.write(f'مشتریان هلو: {len(rows)} | کاربران ناهم‌خوان: {len(found)}')
        for user, reason, row in found:
            target = f"{row.get('Code')} {row.get('Name')} ({row.get('Mobile')})" if row else '-'
            invoices = list(Order.objects.filter(user=user).exclude(holoo_invoice_id__isnull=True).exclude(holoo_invoice_id='')
                            .values_list('holoo_invoice_id', flat=True))
            returns = list(ReturnRequest.objects.filter(user=user).exclude(holoo_invoice_id='').values_list('holoo_invoice_id', flat=True))
            self.stdout.write(self.style.WARNING(
                f'  کاربر #{user.pk} {user.phone_number}: {REASONS[reason]} ← {target}'
                + (f' | فاکتور فروش در هلو: {", ".join(invoices)}' if invoices else '')
                + (f' | فاکتور برگشتی در هلو: {", ".join(returns)}' if returns else '')))

        if not found:
            self.stdout.write(self.style.SUCCESS('همه‌ی کاربران به مشتریِ درست وصل‌اند.'))
            return
        if not opts['fix']:
            self.stdout.write(self.style.WARNING('این فقط گزارش بود؛ برای اصلاح: --fix'))
            return

        from holoo.tasks import sync_user_to_holoo
        ids = unlink_users(found)
        for user_id in ids:
            sync_user_to_holoo.delay(user_id)
        self.stdout.write(self.style.SUCCESS(
            f'{len(ids)} کاربر از مشتریِ اشتباه جدا شدند و همگام‌سازی‌شان به صف رفت. فاکتورهای ثبت‌شده‌ی بالا را دستی در هلو اصلاح کنید.'))
