from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from holoo.conf import get_config


class Command(BaseCommand):
    help = (
        'سفارش‌هایی که در حالت شبیه‌سازی (mock) «ثبت‌شده» علامت خورده‌اند (شماره‌ی INV_ و سند RCP_ ساختگی) و مشتریانی که '
        'کد ساختگی ERP_ گرفته‌اند را پاک‌سازی می‌کند و سفارش‌ها را برای ثبت واقعی در هلو دوباره به صف می‌برد. '
        'بدون --apply فقط گزارش می‌دهد و چیزی تغییر نمی‌کند. فقط وقتی HOLOO_WRITE_MODE=real باشد اجرا می‌شود.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='واقعاً پاک‌سازی و دوباره‌ارسال کن (پیش‌فرض: فقط گزارش)')

    def handle(self, *args, **opts):
        from accounts.models import CustomUser
        from holoo.tasks import send_order_to_holoo
        from orders.models import Order

        config = get_config()
        orders = list(Order.objects.filter(holoo_invoice_id__startswith='INV_').exclude(status__in=('canceled', 'rejected_stock'))
                      .order_by('id'))
        user_ids = {o.user_id for o in orders if o.user_id}
        users = list(CustomUser.objects.filter(erp_code__startswith='ERP_'))

        self.stdout.write(f'سفارش‌های دارای فاکتور ساختگی (فعال): {[o.id for o in orders] or "هیچ"}')
        self.stdout.write(f'کاربران دارای کد مشتریِ ساختگی ERP_: {len(users)} (از این‌ها {len(user_ids & {u.pk for u in users})} نفر صاحب سفارش بالا هستند)')
        self.stdout.write(f'حالت نوشتن: {config.write_mode} | دیتابیس هلو: {config.db_name}')

        if not (orders or users):
            self.stdout.write(self.style.SUCCESS('چیزی برای پاک‌سازی نیست.'))
            return
        if not opts['apply']:
            self.stdout.write(self.style.WARNING('گزارش بود؛ برای اجرا: --apply'))
            return
        if not config.write_is_real:
            raise CommandError('HOLOO_WRITE_MODE=real نیست؛ پاک‌سازی بی‌معناست و سفارش‌ها دوباره mock می‌شدند.')

        with transaction.atomic():
            CustomUser.objects.filter(pk__in=[u.pk for u in users]).update(erp_code=None, holoo_customer_code=None)
            Order.objects.filter(pk__in=[o.pk for o in orders]).update(
                holoo_invoice_id=None, holoo_receipt_id=None, holoo_invoice_erp_code=None,
                holoo_needs_attention=False, holoo_last_error='')
            ids = [o.pk for o in orders]
            transaction.on_commit(lambda: [send_order_to_holoo.delay(i) for i in ids])
        self.stdout.write(self.style.SUCCESS(f'{len(orders)} سفارش پاک‌سازی و برای ثبت واقعی به صف رفت. چند دقیقه بعد diagnose_server را بزنید.'))
