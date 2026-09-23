"""
تست هم‌زمانی واقعی (چند Thread، اتصال دیتابیس مجزا به ازای هر Thread، threading.Barrier) برای
approve()/resubmit_for_review() روی دیتابیس واقعی SQL Server — دقیقاً همان الگوی
orders/tests_coupon_concurrency.py:CouponConcurrencyBase (run_threads).

برخلاف تست‌های ترتیبی accounts/tests_approval.py (که فقط منطق کسب‌وکار را تک‌نخی می‌سنجند)،
اینجا خودِ قفل ردیفی select_for_update در برابر دو تراکنش واقعاً هم‌زمان روی یک ردیف آزموده
می‌شود؛ TransactionTestCase (نه TestCase) لازم است چون هر Thread باید commit واقعی بزند تا
تراکنشِ Threadِ دیگر واقعاً پشت قفل بماند (در TestCase معمولی همه‌چیز داخل یک تراکنشِ رول‌بک‌شونده
است و هیچ Threadی چیزی از دیگری قفل نمی‌بیند).
"""

import threading

from django.db import connection
from django.test import TransactionTestCase

from accounts.models import ApprovalStatus, CustomUser


class ApprovalConcurrencyTests(TransactionTestCase):
    # همه‌ی TransactionTestCaseهای پروژه باید یکسان serialized_rollback باشند؛ وگرنه بعد از
    # flushِ یکی از آن‌ها (که post_migrate را دوباره اجرا می‌کند) بازیابیِ سریال‌شده‌ی کلاس بعدی
    # ContentType تکراری می‌سازد (همان قرارداد مستندشده در cart/tests.py:104-106)
    serialized_rollback = True

    def run_threads(self, jobs):
        """ jobs: فهرست تابع‌های بدون آرگومان؛ همه با Barrier هم‌زمان شروع می‌شوند. خروجی: نتیجه‌ی هرکدام یا استثنا """
        barrier = threading.Barrier(len(jobs))
        results = [None] * len(jobs)

        def worker(index, job):
            try:
                barrier.wait(timeout=30)
                results[index] = job()
            except BaseException as error:                               # noqa: BLE001 - نتیجه‌ی استثنا هم ثبت می‌شود
                results[index] = error
            finally:
                connection.close()                                       # اتصال مخصوص همین نخ

        threads = [threading.Thread(target=worker, args=(i, job)) for i, job in enumerate(jobs)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertFalse(any(t.is_alive() for t in threads), 'یک نخ گیر کرد (احتمال deadlock)')
        return results

    # ----- سناریوی ۱: دو approve() هم‌زمان روی یک کاربر PENDING -----

    def test_two_concurrent_approve_calls_exactly_one_transitions(self):
        user = CustomUser.objects.create_user(
            phone_number='09140001001', first_name='رضا', last_name='کریمی', national_code='1231231231',
        )

        def make_job(level):
            def run():
                # هر Thread نمونه‌ی تازه‌ی خودش را می‌گیرد؛ دقیقاً مثل دو درخواست HTTP مستقل
                fresh = CustomUser.objects.get(pk=user.pk)
                return fresh.approve(price_level=level)
            return run

        results = self.run_threads([make_job(2), make_job(5)])

        for r in results:
            self.assertNotIsInstance(r, BaseException, msg=f'یک نخ استثنا داد: {r!r}')

        changed_flags = sorted(changed for _, changed in results)
        self.assertEqual(changed_flags, [False, True], 'دقیقاً یکی باید changed=True و دیگری False باشد')

        user.refresh_from_db()
        self.assertEqual(user.approval_status, ApprovalStatus.APPROVED)
        # سطح قیمت باید دقیقاً همانِ نخِ برنده باشد (۲ یا ۵)؛ نه مقداری ترکیبی/خراب از رقابت روی ردیف
        self.assertIn(user.price_level, (2, 5))

    # ----- سناریوی ۲: دو resubmit_for_review() هم‌زمان روی یک کاربر REJECTED -----

    def test_two_concurrent_resubmit_calls_exactly_one_transitions(self):
        user = CustomUser.objects.create_user(
            phone_number='09140001002', first_name='مریم', last_name='صادقی', national_code='1231231232',
        )
        user.reject(reason='مدارک ناقص')

        def run():
            fresh = CustomUser.objects.get(pk=user.pk)
            return fresh.resubmit_for_review()

        results = self.run_threads([run, run])

        for r in results:
            self.assertNotIsInstance(r, BaseException, msg=f'یک نخ استثنا داد: {r!r}')

        changed_flags = sorted(changed for _, changed in results)
        self.assertEqual(changed_flags, [False, True], 'دقیقاً یکی باید changed=True و دیگری False باشد (بدون پیامک تکراری)')

        user.refresh_from_db()
        self.assertEqual(user.approval_status, ApprovalStatus.PENDING)
