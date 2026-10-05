import os
import statistics
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.utils import timezone


def _line(out, text=''):
    out.write(text)


def _percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(round((len(values) - 1) * p)))]


def _fmt(seconds):
    return '—' if seconds is None else f'{seconds:.1f}s'


class Command(BaseCommand):
    help = (
        'تشخیص فقط‌خواندنیِ سرور: تنظیمات هلو، Redis/Celery (وضعیت Worker و نوع pool)، اتصال به هلو، آخرین سفارش‌ها و وضعیت '
        'ثبتشان در هلو، و تأخیر/خطای پیامک‌ها. هیچ‌چیز نمی‌نویسد و هیچ پیامکی نمی‌فرستد. خروجی را برای پشتیبان بفرستید.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--orders', type=int, default=10, help='تعداد آخرین سفارش‌ها')
        parser.add_argument('--delivery', type=int, default=0,
                            help='وضعیت تحویل N پیامک اخیر را از ملی‌پیامک بپرس (فقط خواندن؛ پیامکی نمی‌فرستد)')

    def handle(self, *args, **opts):
        out = self.stdout
        problems = []

        self.section_environment(out, problems)
        self.section_infrastructure(out, problems)
        self.section_holoo(out, problems)
        self.section_orders(out, problems, opts['orders'])
        self.section_notifications(out, problems, opts['delivery'])

        out.write('\n== جمع‌بندی ==')
        if problems:
            for problem in problems:
                out.write(self.style.WARNING(f'  ⚠ {problem}'))
        else:
            out.write(self.style.SUCCESS('  مورد مشکوکی پیدا نشد.'))

    # ------------------------------------------------------------------ ۱. محیط
    def section_environment(self, out, problems):
        from holoo.conf import get_config

        out.write('== محیط ==')
        env_file = Path(settings.BASE_DIR) / '.env'
        config = get_config()
        db = settings.DATABASES['default']
        out.write(f'  مسیر پروژه: {settings.BASE_DIR}')
        out.write(f'  فایل .env: {"هست" if env_file.exists() else "نیست"}')
        out.write(f'  DEBUG: {settings.DEBUG}')
        out.write(f'  دیتابیس سایت: {db.get("NAME")} @ {db.get("HOST")}:{db.get("PORT")}')
        out.write(f'  REDIS_URL: {settings.REDIS_URL}')
        out.write(f'  تنظیمات هلو: {config.masked()}')
        for warning in config.warnings:
            out.write(self.style.WARNING(f'  هشدار تنظیمات: {warning}'))
        if config.write_is_mock:
            problems.append('HOLOO_WRITE_MODE=mock است: تأیید سفارش فاکتور واقعی در هلو نمی‌سازد (فقط شماره‌ی INV_ ساختگی). '
                            'در .env سرور HOLOO_WRITE_MODE=real بگذارید و سرویس‌های وب و Celery را ری‌استارت کنید.')
        elif config.write_is_disabled:
            problems.append('نوشتن در هلو غیرفعال است (disabled): سفارش‌های تأییدشده ارسال نمی‌شوند.')
        if config.read_is_mock:
            problems.append('خواندن از هلو روی mock است (HOLOO_READ_MODE).')
        if not env_file.exists() and config.write_is_mock:
            problems.append('فایل .env کنار manage.py وجود ندارد؛ مقدارهای HOLOO_* از پیش‌فرض‌ها خوانده می‌شوند.')

    # ------------------------------------------------------------------ ۲. Redis / Celery
    def section_infrastructure(self, out, problems):
        out.write('\n== Redis و Celery ==')
        started = time.time()
        try:
            cache.set('diagnose:ping', '1', 30)
            ok = cache.get('diagnose:ping') == '1'
            out.write(f'  کش (Redis): {"سالم" if ok else "پاسخ نادرست"} ({_fmt(time.time() - started)})')
            if not ok:
                problems.append('کش Redis درست پاسخ نمی‌دهد.')
        except Exception as error:
            out.write(self.style.ERROR(f'  کش (Redis): خطا - {type(error).__name__}: {error}'))
            problems.append('Redis در دسترس نیست؛ قفل‌ها، تسک‌ها و پیامک‌ها کار نمی‌کنند.')
            return

        try:
            import redis
            client = redis.Redis.from_url(settings.CELERY_BROKER_URL, socket_connect_timeout=3)
            queued = client.llen('celery')
            out.write(f'  طول صف celery (تسک‌های منتظر): {queued}')
            if queued > 20:
                problems.append(f'{queued} تسک در صف منتظرند؛ Worker کند است یا نیست (احتمالاً pool=solo پشت یک تسک سنگین).')
        except Exception as error:
            out.write(self.style.ERROR(f'  broker: خطا - {type(error).__name__}: {error}'))
            problems.append('اتصال به broker (Redis db 0) برقرار نشد.')

        try:
            from config.celery import app
            inspect = app.control.inspect(timeout=10)
            pong = inspect.ping() or {}
            if not pong:
                out.write(self.style.ERROR('  Worker: پاسخ نداد (خاموش است یا پشت تسک سنگین مشغول)'))
                problems.append('هیچ Celery Worker پاسخ نداد؛ تسک‌های هلو و پیامک اجرا نمی‌شوند.')
            else:
                stats = inspect.stats() or {}
                active = inspect.active() or {}
                reserved = inspect.reserved() or {}
                for name in pong:
                    pool = (stats.get(name, {}).get('pool') or {})
                    implementation = pool.get('implementation', '?')
                    concurrency = pool.get('max-concurrency', '?')
                    out.write(f'  Worker {name}: pool={implementation} concurrency={concurrency} '
                              f'در حال اجرا={len(active.get(name, []))} رزروشده={len(reserved.get(name, []))}')
                    for task in active.get(name, []):
                        out.write(f'     ▶ {task.get("name")} (از {_fmt(time.time() - float(task.get("time_start") or time.time()))} پیش)')
                    if 'solo' in str(implementation):
                        problems.append('Worker با pool=solo اجرا می‌شود: فقط یک تسک هم‌زمان؛ سینک سنگین محصولات (چند دقیقه) '
                                        'پیامک ورود و ثبت فاکتور را پشت خودش نگه می‌دارد. با run_celery.bat جدید '
                                        '(--pool=threads --concurrency=8) سرویس را ری‌استارت کنید.')
        except Exception as error:
            out.write(self.style.ERROR(f'  بررسی Worker ناموفق: {type(error).__name__}: {error}'))

        beat_file = Path(settings.BASE_DIR) / 'celerybeat-schedule'
        beat_files = [p for p in Path(settings.BASE_DIR).glob('celerybeat-schedule*') if p.is_file()]
        if beat_files:
            newest = max(beat_files, key=lambda p: p.stat().st_mtime)
            age = time.time() - newest.stat().st_mtime
            out.write(f'  فایل زمان‌بند beat: آخرین تغییر {age / 60:.0f} دقیقه پیش ({newest.name})')
            if age > 3600:
                problems.append('فایل celerybeat بیش از یک ساعت است تغییر نکرده؛ سرویس Beat شاید کار نمی‌کند.')
        else:
            out.write('  فایل زمان‌بند beat پیدا نشد (Beat هرگز اجرا نشده یا در مسیر دیگری است).')

    # ------------------------------------------------------------------ ۳. هلو
    def section_holoo(self, out, problems):
        from holoo.client import HolooClient

        out.write('\n== اتصال به هلو (فقط خواندن) ==')
        client = HolooClient()
        if client.config.read_is_mock:
            out.write('  خواندن mock است؛ تماسی گرفته نشد.')
            return
        started = time.time()
        login = client.login()
        if login.get('status') != 'success':
            out.write(self.style.ERROR(f'  لاگین ناموفق: {login.get("message")}'))
            problems.append(f'لاگین به وب‌سرویس هلو ناموفق: {login.get("message")}')
            return
        out.write(f'  لاگین: موفق ({_fmt(time.time() - started)})')
        started = time.time()
        version = client.get_json('Version')
        out.write(f'  نسخه: {version} ({_fmt(time.time() - started)})')

    # ------------------------------------------------------------------ ۴. سفارش‌ها
    def section_orders(self, out, problems, limit):
        from orders.models import Order

        out.write(f'\n== آخرین {limit} سفارش و ثبت در هلو ==')
        unapproved = Order.objects.filter(approved_at__isnull=True, status__in=('pending', 'registered')).order_by('-id')[:300]
        waiting = sum(1 for o in unapproved if o.customer_status == 'under_review')
        needs = Order.objects.filter(holoo_needs_attention=True).count()
        mock_ids = Order.objects.filter(holoo_invoice_id__startswith='INV_').count()
        approved_no_invoice = Order.objects.filter(approved_at__isnull=False, holoo_invoice_id__isnull=True) \
            .exclude(status__in=('canceled', 'rejected_stock')).count()
        out.write(f'  در انتظار تأیید مدیر: {waiting} | تأییدشده بدون فاکتور هلو: {approved_no_invoice} | '
                  f'نیازمند بررسی: {needs} | با شماره‌ی ساختگی INV_: {mock_ids}')
        if waiting:
            problems.append(f'{waiting} سفارش هنوز «تأیید مدیر» نشده است؛ فاکتور هلو فقط پس از تأیید در پنل ادمین صادر می‌شود.')
        if mock_ids:
            problems.append(f'{mock_ids} سفارش شماره‌ی فاکتور ساختگی (INV_) دارند: در حالت mock ثبت شده‌اند و در هلو نیستند.')
        if needs:
            problems.append(f'{needs} سفارش با خطای دائمی هلو علامت خورده (holoo_needs_attention)؛ علت را در ادمین سفارش ببینید.')
        if approved_no_invoice:
            problems.append(f'{approved_no_invoice} سفارشِ تأییدشده هنوز فاکتور هلو ندارد (Worker/اتصال هلو/خطا؛ لاگ را ببینید).')

        for order in Order.objects.order_by('-id')[:limit]:
            out.write(
                f'  #{order.id} {timezone.localtime(order.created_at):%m-%d %H:%M} وضعیت={order.status} روش={order.payment_method} '
                f'تأیید={"بله" if order.approved_at else "خیر"} فاکتور={order.holoo_invoice_id or "—"} '
                f'سند={order.holoo_receipt_id or "—"} بررسی={"بله" if order.holoo_needs_attention else "خیر"}'
                + (f' خطا={order.holoo_last_error[:80]}' if order.holoo_last_error else '')
            )

    # ------------------------------------------------------------------ ۵. پیامک
    def section_notifications(self, out, problems, delivery_count):
        from notifications.models import Notification
        from products.models import SiteSettings

        out.write('\n== پیامک / اطلاع‌رسانی ==')
        site_backend = SiteSettings.load().notification_backend
        out.write(f'  سرویس فعال در تنظیمات سایت: {site_backend or "(خالی ← پیش‌فرض settings: " + str(settings.NOTIFICATION_BACKEND) + ")"}')
        effective = site_backend or settings.NOTIFICATION_BACKEND
        if 'console' in str(effective).lower():
            problems.append('سرویس اطلاع‌رسانی روی Console است: هیچ پیامکی واقعاً ارسال نمی‌شود. در ادمین ← تنظیمات سایت ← اطلاع‌رسانی '
                            'ملی‌پیامک را انتخاب کنید.')

        since = timezone.now() - timedelta(hours=24)
        recent = Notification.objects.filter(created_at__gte=since)
        by_status = Counter(recent.values_list('status', flat=True))
        by_backend = Counter(recent.exclude(backend='').values_list('backend', flat=True))
        out.write(f'  ۲۴ ساعت اخیر: {dict(by_status) or "هیچ"} | موتورها: {dict(by_backend) or "—"}')

        stuck = Notification.objects.filter(status=Notification.STATUS_PENDING, created_at__lt=timezone.now() - timedelta(minutes=2)).count()
        failed = Notification.objects.filter(status=Notification.STATUS_FAILED, created_at__gte=since).count()
        out.write(f'  pending بیش از ۲ دقیقه: {stuck} | failed (۲۴ ساعت): {failed}')
        if stuck:
            problems.append(f'{stuck} پیام بیش از ۲ دقیقه در حالت pending مانده؛ تسک ارسال اجرا نشده (Worker/Redis).')
        for error, count in Counter(
            recent.filter(status=Notification.STATUS_FAILED).exclude(error='').values_list('error', flat=True)
        ).most_common(3):
            out.write(f'     خطای پرتکرار ({count}×): {error[:140]}')

        for label, queryset in (('همه‌ی پیام‌ها', Notification.objects.filter(status=Notification.STATUS_SENT, created_at__gte=since)),
                                ('کد ورود (otp)', Notification.objects.filter(status=Notification.STATUS_SENT, template_key='otp', created_at__gte=since))):
            delays = [(n.sent_at - n.created_at).total_seconds() for n in queryset.exclude(sent_at__isnull=True).order_by('-id')[:200]]
            if delays:
                out.write(f'  تأخیر سمت ما، {label} (ایجاد ← ارسال به سرویس): میانه {_fmt(statistics.median(delays))} | '
                          f'p95 {_fmt(_percentile(delays, 0.95))} | بیشینه {_fmt(max(delays))} (از {len(delays)} پیام)')
                if _percentile(delays, 0.95) and _percentile(delays, 0.95) > 20:
                    problems.append(f'تأخیر سمت سرور برای «{label}» بالاست (p95 {_percentile(delays, 0.95):.0f}s): تسک‌ها پشت هم صف می‌کشند '
                                    'یا ارسال‌ها retry می‌شوند (فاصله‌ی retry پیش‌فرض ۱ دقیقه).')
            else:
                out.write(f'  {label}: پیام ارسال‌شده‌ای در ۲۴ ساعت اخیر نیست.')
        retried = Notification.objects.filter(created_at__gte=since, attempts__gt=1).count()
        out.write(f'  پیام‌هایی که بیش از یک بار تلاش شده‌اند (۲۴ ساعت): {retried}')

        if delivery_count:
            self.delivery_status(out, delivery_count)

    def delivery_status(self, out, count):
        """ وضعیت تحویل به گوشی از ملی‌پیامک (GetDeliveries2؛ فقط خواندن). ۱ = رسیده به گوشی، ۸ = رسیده به مخابرات، ۱۶/۱۴ = نرسیده """
        import requests
        from notifications.models import Notification

        labels = {'0': 'ارسال نشده', '1': 'رسیده به گوشی', '2': 'نرسیده به گوشی', '4': 'ارسال شده به مخابرات', '8': 'رسیده به مخابرات',
                  '16': 'نرسیده به مخابرات', '32': 'رد شده توسط اپراتور', '64': 'سیاه‌لیست/رد شده'}
        out.write(f'\n  وضعیت تحویل {count} پیام اخیر (ملی‌پیامک):')
        sent = Notification.objects.filter(status=Notification.STATUS_SENT).exclude(provider_message_id='').order_by('-id')[:count]
        for notification in sent:
            try:
                response = requests.post(
                    'https://rest.payamak-panel.com/api/SendSMS/GetDeliveries2',
                    data={'username': settings.MELIPAYAMAK_USERNAME, 'password': settings.MELIPAYAMAK_APIKEY,
                          'recID': notification.provider_message_id}, timeout=15)
                value = str(response.json().get('RetStatus') == 1 and response.json().get('Value'))
                out.write(f'     #{notification.id} {notification.template_key} → وضعیت {value} ({labels.get(value, "؟")}) '
                          f'| ارسال {timezone.localtime(notification.sent_at):%m-%d %H:%M:%S}')
            except Exception as error:
                out.write(f'     #{notification.id}: استعلام ناموفق ({type(error).__name__})')
