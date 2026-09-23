"""
تست‌های migration 0012 (backfill تأیید تجاری کاربران موجود) — دقیقاً الگوی
products.tests.SiteSettingsGuestPricingMigrationTests: TransactionTestCase + MigrationExecutor،
ساخت داده با مدل‌های *تاریخی* (apps.get_model) در نسخه‌ی پیش از 0012، سپس migrate به 0012 و
assert روی نتیجه. هیچ‌کدام از این تست‌ها manage.py migrate را روی دیتابیس dev واقعی اجرا
نمی‌کنند؛ فقط دیتابیس تستی خودِ جنگو (test_HolooWebDB) را طی self.assertRaises/migrate جابه‌جا
می‌کنند و در tearDown به آخرین نسخه برمی‌گردانند.
"""

from importlib import import_module

from django.apps import apps as live_apps
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class BackfillApprovalMigrationTests(TransactionTestCase):
    # همه‌ی TransactionTestCaseهای پروژه باید یکسان serialized_rollback باشند؛ وگرنه بعد از
    # flushِ یکی از آن‌ها (که post_migrate را دوباره اجرا می‌کند) بازیابیِ سریال‌شده‌ی کلاس بعدی
    # ContentType تکراری می‌سازد (قرارداد مستندشده در cart/tests.py:104-106؛ همان چیزی که با
    # حذف اشتباهی این خط این‌جا کل مجموعه‌ی تست پروژه را می‌شکست — رفع شد)
    serialized_rollback = True

    # نسخه‌ی درست-پیش‌از-۰۰۱۲: accounts روی 0011 (approval_status وجود دارد ولی هنوز backfill نشده)،
    # orders/payments روی آخرین نسخه‌شان (چون تابع migration به Order/Transaction کامل نیاز دارد)
    PRE_TARGETS = [
        ('accounts', '0011_customuser_approval_status_customuser_approved_at_and_more'),
        ('orders', '0011_order_coupon_snapshot'),
        ('payments', '0001_initial'),
    ]
    POST_TARGET = ('accounts', '0012_backfill_existing_customer_approval')

    def tearDown(self):
        MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())

    def _migrate(self, targets):
        MigrationExecutor(connection).migrate(targets)
        return MigrationExecutor(connection).loader.project_state(targets).apps

    # ----- کمک‌کننده‌ها (فقط با مدل‌های تاریخی) -----

    def _make_user(self, CustomUser, phone, complete_profile, status='ACTIVE', approval_status='PENDING'):
        return CustomUser.objects.create(
            phone_number=phone, password='!',
            first_name=('نام' if complete_profile else ''),
            last_name=('خانوادگی' if complete_profile else ''),
            national_code=('1234567890' if complete_profile else ''),
            status=status, approval_status=approval_status, price_level=1,
        )

    def _make_order(self, Order, Transaction, user, order_status='delivered', txn_status=None):
        order = Order.objects.create(
            user=user, first_name='گیرنده', last_name='تست', phone='09120000000',
            address='آدرس تست', total_price=100000, status=order_status,
        )
        if txn_status is not None:
            Transaction.objects.create(
                user=user, order=order, amount=100000, authority=f'A-TEST-{order.pk}-{txn_status}', status=txn_status,
            )
        return order

    # ----- ۸ سناریو -----

    def test_1_incomplete_profile_stays_pending_even_with_paid_order(self):
        old_apps = self._migrate(self.PRE_TARGETS)
        CustomUser = old_apps.get_model('accounts', 'CustomUser')
        Order = old_apps.get_model('orders', 'Order')
        Transaction = old_apps.get_model('payments', 'Transaction')

        user = self._make_user(CustomUser, '09150001001', complete_profile=False)
        self._make_order(Order, Transaction, user, txn_status='success')

        new_apps = self._migrate([self.POST_TARGET])
        fresh = new_apps.get_model('accounts', 'CustomUser').objects.get(pk=user.pk)
        self.assertEqual(fresh.approval_status, 'PENDING')

    def test_2_complete_profile_active_without_any_order_stays_pending(self):
        old_apps = self._migrate(self.PRE_TARGETS)
        CustomUser = old_apps.get_model('accounts', 'CustomUser')
        user = self._make_user(CustomUser, '09150001002', complete_profile=True)

        new_apps = self._migrate([self.POST_TARGET])
        fresh = new_apps.get_model('accounts', 'CustomUser').objects.get(pk=user.pk)
        self.assertEqual(fresh.approval_status, 'PENDING')

    def test_3_only_canceled_order_stays_pending(self):
        old_apps = self._migrate(self.PRE_TARGETS)
        CustomUser = old_apps.get_model('accounts', 'CustomUser')
        Order = old_apps.get_model('orders', 'Order')
        Transaction = old_apps.get_model('payments', 'Transaction')

        user = self._make_user(CustomUser, '09150001003', complete_profile=True)
        self._make_order(Order, Transaction, user, order_status='canceled', txn_status='success')

        new_apps = self._migrate([self.POST_TARGET])
        fresh = new_apps.get_model('accounts', 'CustomUser').objects.get(pk=user.pk)
        self.assertEqual(fresh.approval_status, 'PENDING')

    def test_4_order_with_only_pending_or_failed_transaction_stays_pending(self):
        old_apps = self._migrate(self.PRE_TARGETS)
        CustomUser = old_apps.get_model('accounts', 'CustomUser')
        Order = old_apps.get_model('orders', 'Order')
        Transaction = old_apps.get_model('payments', 'Transaction')

        user = self._make_user(CustomUser, '09150001004', complete_profile=True)
        self._make_order(Order, Transaction, user, txn_status='pending')
        self._make_order(Order, Transaction, user, txn_status='failed')

        new_apps = self._migrate([self.POST_TARGET])
        fresh = new_apps.get_model('accounts', 'CustomUser').objects.get(pk=user.pk)
        self.assertEqual(fresh.approval_status, 'PENDING')

    def test_5_eligible_user_gets_approved_and_price_level_is_preserved(self):
        old_apps = self._migrate(self.PRE_TARGETS)
        CustomUser = old_apps.get_model('accounts', 'CustomUser')
        Order = old_apps.get_model('orders', 'Order')
        Transaction = old_apps.get_model('payments', 'Transaction')

        user = self._make_user(CustomUser, '09150001005', complete_profile=True)
        CustomUser.objects.filter(pk=user.pk).update(price_level=4)   # سطح دلخواه/غیر-پیش‌فرض
        self._make_order(Order, Transaction, user, txn_status='success')

        new_apps = self._migrate([self.POST_TARGET])
        fresh = new_apps.get_model('accounts', 'CustomUser').objects.get(pk=user.pk)
        self.assertEqual(fresh.approval_status, 'APPROVED')
        self.assertIsNotNone(fresh.approved_at)
        self.assertEqual(fresh.price_level, 4)   # دست‌نخورده، نه ریست‌شده

    def test_6_rejected_user_is_never_touched_even_if_otherwise_eligible(self):
        old_apps = self._migrate(self.PRE_TARGETS)
        CustomUser = old_apps.get_model('accounts', 'CustomUser')
        Order = old_apps.get_model('orders', 'Order')
        Transaction = old_apps.get_model('payments', 'Transaction')

        user = self._make_user(CustomUser, '09150001006', complete_profile=True, approval_status='REJECTED')
        self._make_order(Order, Transaction, user, txn_status='success')

        new_apps = self._migrate([self.POST_TARGET])
        fresh = new_apps.get_model('accounts', 'CustomUser').objects.get(pk=user.pk)
        self.assertEqual(fresh.approval_status, 'REJECTED')   # دست‌نخورده
        self.assertIsNone(fresh.approved_at)

    def test_7_running_the_function_twice_is_idempotent(self):
        old_apps = self._migrate(self.PRE_TARGETS)
        CustomUser = old_apps.get_model('accounts', 'CustomUser')
        Order = old_apps.get_model('orders', 'Order')
        Transaction = old_apps.get_model('payments', 'Transaction')

        user = self._make_user(CustomUser, '09150001007', complete_profile=True)
        self._make_order(Order, Transaction, user, txn_status='success')

        new_apps = self._migrate([self.POST_TARGET])            # اجرای اول (خودِ migration)
        LiveCustomUser = new_apps.get_model('accounts', 'CustomUser')
        after_first = LiveCustomUser.objects.get(pk=user.pk)
        self.assertEqual(after_first.approval_status, 'APPROVED')
        approved_at_after_first = after_first.approved_at

        module = import_module('accounts.migrations.0012_backfill_existing_customer_approval')
        module.backfill_approved_customers(live_apps, None)     # اجرای دوم، مستقیم روی دیتابیسِ مایگریت‌شده

        after_second = live_apps.get_model('accounts', 'CustomUser').objects.get(pk=user.pk)
        self.assertEqual(after_second.approval_status, 'APPROVED')
        # چون این کاربر دیگر PENDING نیست، اجرای دوم اصلاً کاندیدش نمی‌کند؛ approved_at دست‌نخورده می‌ماند
        self.assertEqual(after_second.approved_at, approved_at_after_first)

    def test_8_reverse_migration_does_not_revert_approved_status(self):
        old_apps = self._migrate(self.PRE_TARGETS)
        CustomUser = old_apps.get_model('accounts', 'CustomUser')
        Order = old_apps.get_model('orders', 'Order')
        Transaction = old_apps.get_model('payments', 'Transaction')

        user = self._make_user(CustomUser, '09150001008', complete_profile=True)
        self._make_order(Order, Transaction, user, txn_status='success')

        self._migrate([self.POST_TARGET])                       # فوروارد: کاربر APPROVED می‌شود
        reverted_apps = self._migrate(self.PRE_TARGETS)          # بازگشت (reverse) به قبل از 0012

        fresh = reverted_apps.get_model('accounts', 'CustomUser').objects.get(pk=user.pk)
        # RunPython.noop یعنی هیچ کدی روی reverse اجرا نمی‌شود؛ داده‌ی نوشته‌شده توسط forward دست‌نخورده می‌ماند
        self.assertEqual(fresh.approval_status, 'APPROVED')
