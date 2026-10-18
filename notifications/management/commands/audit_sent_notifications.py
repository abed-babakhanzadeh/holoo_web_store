from collections import Counter

from django.core.management.base import BaseCommand
from django.db.models.functions import Length

from notifications.backends.melipayamak import ERROR_CODES, MAX_ERROR_CODE_LENGTH
from notifications.models import Notification


class Command(BaseCommand):
    help = (
        'پیام‌هایی را پیدا می‌کند که «ارسال‌شده» ثبت شده‌اند ولی ملی‌پیامک در واقع آن‌ها را رد کرده بود (به‌جای شناسه‌ی پیام، یک کد کوتاه خطا '
        'در شناسه‌ی سرویس‌دهنده ذخیره شده؛ مثلاً ۱۴ = متن حاوی لینک). پیش‌فرض فقط گزارش می‌دهد؛ با --fix وضعیتشان «ناموفق» و دلیل نوشته می‌شود.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--fix', action='store_true', help='وضعیت این پیام‌ها را به «ناموفق» با دلیل تغییر بده')

    def handle(self, *args, **opts):
        suspicious = (Notification.objects.filter(status=Notification.STATUS_SENT, backend__icontains='melipayamak')
                      .annotate(id_length=Length('provider_message_id')).filter(id_length__gt=0, id_length__lte=MAX_ERROR_CODE_LENGTH)
                      .order_by('created_at'))
        rows = [n for n in suspicious if n.provider_message_id.isdigit()]
        self.stdout.write(f'پیام‌های «ارسال‌شده»ی مشکوک (کد خطا به‌جای شناسه): {len(rows)}')
        counts = Counter((n.template_key, n.provider_message_id) for n in rows)
        for (template, code), count in counts.most_common():
            message = ERROR_CODES.get(code.lstrip('0') or '0', ('کد نامشخص', False))[0]
            self.stdout.write(f'  {template} — کد {code} ({message}): {count} پیام')
        if not rows:
            return
        if not opts['fix']:
            self.stdout.write(self.style.WARNING('این فقط گزارش بود؛ برای اصلاح وضعیت: --fix'))
            return
        for n in rows:
            message = ERROR_CODES.get(n.provider_message_id.lstrip('0') or '0', ('کد نامشخص', False))[0]
            n.status = Notification.STATUS_FAILED
            n.error = f'ملی‌پیامک پیام را نفرستاد (کد {n.provider_message_id}): {message}'
            n.save(update_fields=['status', 'error'])
        self.stdout.write(self.style.SUCCESS(f'{len(rows)} پیام به «ناموفق» تغییر کرد.'))
