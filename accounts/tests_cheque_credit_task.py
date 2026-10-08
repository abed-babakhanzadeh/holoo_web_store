"""
فاز F4: تسک دوره‌ای پاک‌سازی مدارک درخواست خرید چکی (accounts.tasks.purge_cheque_credit_documents) و زمان‌بندی آن در Celery Beat.
منطق پاک‌سازی در accounts/tests_cheque_credit.py (PurgeTests) تست شده؛ اینجا پوشش تسک، احترام به تنظیم ادمین و زمان‌بندی است.
MEDIA_ROOT هر تست موقت است.
"""
from datetime import timedelta
from unittest import mock

from celery.schedules import crontab
from django.utils import timezone

from accounts import cheque_credit_service as service
from accounts.cheque_credit import ChequeCreditRequest
from accounts.tasks import purge_cheque_credit_documents
from accounts.tests_cheque_credit import CreditBase, docs, make_user, set_retention, stored


class PurgeTaskTests(CreditBase):
    def decided(self, days_ago, finish='reject'):
        user = make_user(f'0912000{700 + ChequeCreditRequest.objects.count():04d}', price_level=2)
        request = self.submit(user=user, files=docs('cheque_book', 'national_card'))
        if finish == 'reject':
            service.reject_request(request, self.admin, 'x')
        elif finish == 'approve':
            service.approve_request(request, self.admin)
        else:
            service.cancel_request(request, user)
        ChequeCreditRequest.objects.filter(pk=request.pk).update(decided_at=timezone.now() - timedelta(days=days_ago))
        return request

    def run_task(self):
        with self.captureOnCommitCallbacks(execute=True):
            return purge_cheque_credit_documents()

    def test_the_task_purges_old_decided_documents_and_reports_the_count(self):
        old = [self.decided(40), self.decided(31, 'approve'), self.decided(90, 'cancel')]
        keep = self.decided(5)
        self.assertEqual(self.run_task(), 'purged=3')
        for request in old:
            self.assertEqual(self.reload(request).documents.count(), 0)
        self.assertEqual(self.reload(keep).documents.count(), 2)
        self.assertEqual(len(stored(self.media_root)), 2)                           # فقط تصاویر درخواست تازه
        self.assertEqual(self.run_task(), 'purged=0')

    def test_zero_days_disables_the_purge(self):
        request = self.decided(500)
        set_retention(0)
        self.assertEqual(self.run_task(), 'purged=0')
        self.assertEqual(self.reload(request).documents.count(), 2)
        self.assertIsNone(self.reload(request).documents_purged_at)

    def test_the_setting_is_read_on_every_run(self):
        request = self.decided(10)
        self.assertEqual(self.run_task(), 'purged=0')                               # پیش‌فرض ۳۰ روز
        set_retention(7)
        self.assertEqual(self.run_task(), 'purged=1')
        self.assertEqual(self.reload(request).documents.count(), 0)

    def test_pending_requests_are_never_purged(self):
        request = self.submit()
        ChequeCreditRequest.objects.filter(pk=request.pk).update(decided_at=timezone.now() - timedelta(days=999))
        self.assertEqual(self.run_task(), 'purged=0')
        self.assertEqual(self.reload(request).documents.count(), 1)

    def test_the_task_keeps_going_through_several_batches(self):
        for _ in range(5):
            self.decided(60)
        with mock.patch.object(service, 'PURGE_BATCH', 2):
            real = service.purge_expired_documents
            with mock.patch.object(service, 'purge_expired_documents', side_effect=lambda **kw: real(limit=2)):
                self.assertEqual(self.run_task(), 'purged=5')

    def test_one_failing_request_does_not_loop_forever(self):
        from accounts.cheque_credit import ChequeCreditDocument
        first = self.decided(60)
        second = self.decided(59)
        real = ChequeCreditDocument.delete

        def flaky(doc, *args, **kwargs):
            if doc.request_id == first.pk:
                raise RuntimeError('boom')
            return real(doc, *args, **kwargs)
        with mock.patch.object(ChequeCreditDocument, 'delete', flaky), self.assertLogs('accounts.cheque_credit_service', level='ERROR'):
            self.assertEqual(self.run_task(), 'purged=1')
        self.assertEqual(self.reload(second).documents.count(), 0)
        self.assertEqual(self.reload(first).documents.count(), 2)


class ScheduleTests(CreditBase):
    def test_the_task_is_scheduled_nightly_in_celery_beat(self):
        from config.celery import app
        app.finalize()
        entry = app.conf.beat_schedule['purge-cheque-credit-documents']
        self.assertEqual(entry['task'], 'accounts.tasks.purge_cheque_credit_documents')
        schedule = entry['schedule']
        self.assertIsInstance(schedule, crontab)
        self.assertEqual((schedule.hour, schedule.minute), ({3}, {25}))

    def test_the_task_is_registered_with_celery(self):
        from config.celery import app
        app.loader.import_default_modules()
        self.assertIn('accounts.tasks.purge_cheque_credit_documents', app.tasks)
