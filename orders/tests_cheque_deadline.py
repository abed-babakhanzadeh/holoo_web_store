"""
فاز D چکی: مهلت ثبت اطلاعات چک (تنظیم ادمین، پیش‌فرض ۲۴ ساعت)، ثبت مهلت روی سفارش چکی، نمایش مهلت/شمارنده به مشتری،
تسک دوره‌ای لغو خودکار + آزادسازی موجودی، و توقف مهلت با ثبت اولین چک.
MEDIA_ROOT هر تست موقت است؛ به media/ واقعی چیزی نوشته نمی‌شود.
"""
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.utils import timezone

from orders import cheques, deadline
from orders.models import ChequePayment, Order, OrderItem
from orders.signals import order_canceled
from orders.tasks import cancel_expired_cheque_orders as cancel_task
from orders.tests_cheques import ChequeFlowBase, VALID, make_image, upload
from orders.tests_cheque_review import OTHER
from products import stock
from products.models import SiteSettings, StockReservation
from django.test import TestCase


def set_hours(hours):
    settings_obj = SiteSettings.load()
    settings_obj.cheque_submission_deadline_hours = hours
    settings_obj.save()
    cache.delete(SiteSettings.CACHE_KEY)


class DeadlineBase(ChequeFlowBase):
    def setUp(self):
        super().setUp()
        self.now = timezone.now()

    def make_order(self, *, expired=True, qty=2, **fields):
        """ سفارش چکیِ باز با ردیف و رزرو؛ expired=True ← مهلت یک ساعت پیش تمام شده """
        fields.setdefault('cheque_deadline_at', self.now - timedelta(hours=1) if expired else self.now + timedelta(hours=5))
        order = self.cheque_order(**fields)
        OrderItem.objects.create(order=order, product=self.product, price=100000, quantity=qty)
        with stock.transaction.atomic():
            stock.reserve_for_order(order.id, {self.product.pk: qty})
        return order

    def reload(self, obj):
        return type(obj).objects.get(pk=obj.pk)

    def add_cheque(self, order, status='pending_review', sayadi=VALID):
        return ChequePayment.objects.create(order=order, sayadi_id=sayadi, status=status)

    def reserved(self):
        return self.reload(self.product).reserved_quantity

    def sweep(self, **kwargs):
        with self.captureOnCommitCallbacks(execute=True):
            return deadline.cancel_expired_cheque_orders(now=self.now, **kwargs)


# ------------------------------------------------------------------ تنظیم ادمین
class SettingTests(TestCase):
    def test_the_default_is_24_hours(self):
        self.addCleanup(cache.delete, SiteSettings.CACHE_KEY)
        self.assertEqual(SiteSettings.load().cheque_submission_deadline_hours, 24)

    def test_bounds_are_validated(self):
        obj = SiteSettings.load()
        obj.cheque_submission_deadline_hours = 721
        with self.assertRaises(ValidationError):
            obj.full_clean(exclude=[f.name for f in obj._meta.fields if f.name != 'cheque_submission_deadline_hours'])
        obj.cheque_submission_deadline_hours = 0
        obj.full_clean(exclude=[f.name for f in obj._meta.fields if f.name != 'cheque_submission_deadline_hours'])

    def test_the_setting_is_editable_in_the_site_settings_admin(self):
        from django.contrib.admin.sites import site
        admin_obj = site._registry[SiteSettings]
        fields = [f for _, opts in admin_obj.get_fieldsets(None) for f in opts['fields']]
        self.assertIn('cheque_submission_deadline_hours', fields)


# ------------------------------------------------------------------ ثبت مهلت روی سفارش
class DeadlineStampTests(ChequeFlowBase):
    def place(self, option):
        with mock.patch('holoo.receivers.send_order_to_holoo'):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.post_order({'address_id': self.address.pk, 'payment_method': option})
        self.assertEqual(response.status_code, 302)
        return Order.objects.filter(user=self.user).latest('id')

    def test_a_cheque_order_gets_now_plus_24_hours(self):
        before = timezone.now()
        order = self.place('check')
        after = timezone.now()
        self.assertGreaterEqual(order.cheque_deadline_at, before + timedelta(hours=24) - timedelta(seconds=5))
        self.assertLessEqual(order.cheque_deadline_at, after + timedelta(hours=24) + timedelta(seconds=5))

    def test_the_configured_hours_are_used(self):
        set_hours(6)
        order = self.place('check')
        delta = order.cheque_deadline_at - order.created_at
        self.assertAlmostEqual(delta.total_seconds(), 6 * 3600, delta=30)

    def test_a_cash_order_has_no_deadline(self):
        self.assertIsNone(self.place('cash').cheque_deadline_at)

    def test_a_disabled_setting_stamps_nothing(self):
        set_hours(0)
        self.assertIsNone(self.place('check').cheque_deadline_at)

    def test_changing_the_setting_later_does_not_move_existing_deadlines(self):
        order = self.place('check')
        stamped = order.cheque_deadline_at
        set_hours(1)
        self.assertEqual(Order.objects.get(pk=order.pk).cheque_deadline_at, stamped)

    def test_a_vip_cheque_order_gets_a_deadline_too(self):
        from products.pricing import VIP_CHEQUE_VIP_PRICE
        from orders.tests_payment_options import set_policy
        self.user.price_level = 3
        self.user.save(update_fields=['price_level'])
        set_policy(VIP_CHEQUE_VIP_PRICE)
        order = self.place('vip_check')
        self.assertEqual((order.payment_method, order.settlement), ('vip', 'cheque'))
        self.assertIsNotNone(order.cheque_deadline_at)

    def test_the_admin_sees_the_deadline_read_only(self):
        from accounts.testing import make_approved_user
        from django.test import Client
        from django.urls import reverse
        order = self.place('check')
        admin = Client()
        admin.force_login(make_approved_user('09120000080', is_staff=True, is_superuser=True))
        page = admin.get(reverse('admin:orders_order_change', args=[order.id]))
        self.assertContains(page, 'مهلت ثبت اطلاعات چک')
        self.assertNotContains(page, 'name="cheque_deadline_at"')


# ------------------------------------------------------------------ لغو خودکار
class AutoCancelTests(DeadlineBase):
    def test_an_expired_cheque_order_without_cheques_is_canceled_and_stock_released(self):
        order = self.make_order()
        self.assertEqual(self.reserved(), 2)
        self.assertEqual(self.sweep(), 1)
        order = self.reload(order)
        self.assertEqual((order.status, order.cancel_reason), ('canceled', deadline.CANCEL_REASON))
        self.assertIsNotNone(order.canceled_at)
        self.assertEqual(self.reserved(), 0)
        self.assertFalse(StockReservation.objects.filter(order_id=order.id, state=StockReservation.HELD).exists())
        self.assertTrue(StockReservation.objects.filter(order_id=order.id, state=StockReservation.RELEASED).exists())
        self.assertEqual(order.customer_status, 'canceled')

    def test_the_canceled_signal_fires_once(self):
        order = self.make_order()
        seen = []
        handler = lambda sender, order, **kw: seen.append(order.pk)          # noqa: E731
        order_canceled.connect(handler, weak=False, dispatch_uid='deadline_test_seen')
        self.addCleanup(order_canceled.disconnect, dispatch_uid='deadline_test_seen')
        self.sweep()
        self.sweep()
        self.assertEqual(seen, [order.pk])

    def test_an_order_within_its_deadline_is_left_alone(self):
        order = self.make_order(expired=False)
        self.assertEqual(self.sweep(), 0)
        self.assertEqual(self.reload(order).status, 'pending')
        self.assertEqual(self.reserved(), 2)

    def test_the_boundary_is_inclusive(self):
        order = self.make_order(cheque_deadline_at=self.now)
        self.assertEqual(self.sweep(), 1)
        self.assertEqual(self.reload(order).status, 'canceled')

    def test_an_active_cheque_protects_the_order(self):
        for status in ('pending_review', 'approved'):
            with self.subTest(status=status):
                order = self.make_order()
                self.add_cheque(order, status, sayadi=f'62198610{ChequePayment.objects.count():08d}')
                self.assertEqual(self.sweep(), 0)
                self.assertEqual(self.reload(order).status, 'pending')

    def test_rejected_or_withdrawn_cheques_do_not_protect_the_order(self):
        """ فاز E: فقط چکِ فعال (pending/approved) مهلت را متوقف می‌کند؛ رد/حذف سفارش را بدون چک می‌گذارد """
        for status in ('rejected', 'withdrawn'):
            with self.subTest(status=status):
                order = self.make_order()
                self.add_cheque(order, status, sayadi=f'62198610{ChequePayment.objects.count():08d}')
                self.assertEqual(self.sweep(), 1)
                order = self.reload(order)
                self.assertEqual((order.status, order.cancel_reason), ('canceled', deadline.CANCEL_REASON_CORRECTION))

    def test_a_cheque_registered_through_the_service_stops_the_clock(self):
        order = self.make_order()
        cheques.create_cheque(order, self.user, {'sayadi_id': VALID}, [upload(make_image())])
        self.assertIsNone(self.reload(order).cheque_deadline_at)                  # ساعت متوقف شد
        self.assertEqual(self.sweep(), 0)
        self.assertEqual(self.reload(order).status, 'pending')

    def test_only_cheque_orders_are_touched(self):
        cash = self.make_order(payment_method='cash', settlement='online')
        self.assertEqual(self.sweep(), 0)
        self.assertEqual(self.reload(cash).status, 'pending')

    def test_orders_without_a_deadline_are_never_canceled(self):
        order = self.make_order(cheque_deadline_at=None)
        Order.objects.filter(pk=order.pk).update(cheque_deadline_at=None)
        self.assertEqual(self.sweep(), 0)
        self.assertEqual(self.reload(order).status, 'pending')

    def test_approved_canceled_and_rejected_orders_are_skipped(self):
        approved = self.make_order(approved_at=self.now)
        already = self.make_order(status='canceled')
        rejected = self.make_order(status='rejected_stock')
        self.assertEqual(self.sweep(), 0)
        self.assertIsNone(self.reload(approved).canceled_at)
        self.assertEqual(self.reload(already).cancel_reason, '')
        self.assertEqual(self.reload(rejected).status, 'rejected_stock')

    def test_a_paid_cheque_order_is_skipped(self):
        from payments.models import Transaction
        order = self.make_order()
        Transaction.objects.create(user=self.user, order=order, amount=500000, status='success', authority='AUTH-DL-1')
        self.assertEqual(self.sweep(), 0)
        self.assertEqual(self.reload(order).status, 'pending')

    def test_a_disabled_setting_cancels_nothing(self):
        order = self.make_order()
        set_hours(0)
        self.assertEqual(self.sweep(), 0)
        self.assertEqual(self.reload(order).status, 'pending')
        self.assertEqual(self.reserved(), 2)

    def test_the_batch_limit_and_idempotence(self):
        orders = [self.make_order(qty=1) for _ in range(3)]
        self.assertEqual(self.sweep(limit=2), 2)
        self.assertEqual(self.sweep(limit=2), 1)
        self.assertEqual(self.sweep(), 0)
        self.assertEqual(sorted(self.reload(o).status for o in orders), ['canceled'] * 3)
        self.assertEqual(self.reserved(), 0)

    def test_one_failing_order_does_not_stop_the_sweep(self):
        first = self.make_order(cheque_deadline_at=self.now - timedelta(hours=3))
        second = self.make_order(cheque_deadline_at=self.now - timedelta(hours=2))
        real = deadline.cancel_if_expired

        def flaky(order_id, now=None):
            if order_id == first.pk:
                raise RuntimeError('boom')
            return real(order_id, now)
        with mock.patch('orders.deadline.cancel_if_expired', side_effect=flaky):
            with self.assertLogs('orders.deadline', level='ERROR'):
                self.assertEqual(self.sweep(), 1)
        self.assertEqual(self.reload(first).status, 'pending')
        self.assertEqual(self.reload(second).status, 'canceled')

    def test_the_celery_task_wraps_the_sweep(self):
        self.make_order(cheque_deadline_at=timezone.now() - timedelta(hours=1))
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(cancel_task(), 'canceled=1')
        self.assertEqual(cancel_task(), 'canceled=0')

    def test_the_task_is_scheduled_every_15_minutes(self):
        from config.celery import app
        app.finalize()
        entry = app.conf.beat_schedule['cancel-expired-cheque-orders']
        self.assertEqual(entry['task'], 'orders.tasks.cancel_expired_cheque_orders')
        self.assertEqual(entry['schedule'], 15 * 60.0)


# ------------------------------------------------------------------ مسابقه‌ها
class RaceTests(DeadlineBase):
    def test_registering_a_cheque_after_the_cancel_is_refused(self):
        order = self.make_order()
        self.sweep()
        with self.assertRaises(cheques.ChequeError) as caught:
            cheques.create_cheque(self.reload(order), self.user, {'sayadi_id': VALID}, [upload(make_image())])
        self.assertEqual(caught.exception.status, 409)
        self.assertFalse(ChequePayment.objects.filter(order=order).exists())

    def test_cancel_rechecks_under_the_lock_and_sees_a_cheque_added_after_the_scan(self):
        order = self.make_order()
        self.add_cheque(order)                                  # بعد از انتخاب کاندید و قبل از قفل ثبت شد
        self.assertFalse(deadline.cancel_if_expired(order.pk, self.now))
        self.assertEqual(self.reload(order).status, 'pending')

    def test_a_late_cheque_before_the_sweep_is_still_accepted(self):
        order = self.make_order()                               # مهلت گذشته ولی هنوز لغو نشده
        cheques.create_cheque(order, self.user, {'sayadi_id': VALID}, [upload(make_image())])
        self.assertEqual(self.sweep(), 0)
        self.assertEqual(self.reload(order).status, 'pending')


# ------------------------------------------------------------------ نمایش به مشتری
class NoticeTests(DeadlineBase):
    def notice(self, order):
        return deadline.deadline_notice(self.reload(order), now=self.now)

    def test_remaining_time_text(self):
        for seconds, expected in ((59, '1 دقیقه'), (3600, '1 ساعت'), (9000, '2 ساعت و 30 دقیقه'), (86400 + 7200, '1 روز و 2 ساعت'),
                                  (30 * 60, '30 دقیقه')):
            with self.subTest(seconds=seconds):
                self.assertEqual(deadline.format_remaining(seconds), expected)

    def test_an_open_order_without_cheques_shows_the_countdown(self):
        order = self.make_order(expired=False, cheque_deadline_at=self.now + timedelta(hours=5, minutes=30))
        info = self.notice(order)
        self.assertFalse(info['expired'])
        self.assertIn('5 ساعت و 30 دقیقه', info['text'])
        self.assertEqual(info['seconds'], 5 * 3600 + 30 * 60)

    def test_an_expired_but_not_yet_canceled_order_says_so(self):
        info = self.notice(self.make_order())
        self.assertTrue(info['expired'])
        self.assertIn('به پایان رسیده', info['text'])

    def test_no_notice_once_a_cheque_exists_or_the_order_is_closed_or_disabled(self):
        with_cheque = self.make_order(expired=False)
        self.add_cheque(with_cheque)
        self.assertIsNone(self.notice(with_cheque))
        self.assertIsNone(self.notice(self.make_order(expired=False, status='canceled')))
        self.assertIsNone(self.notice(self.make_order(expired=False, approved_at=self.now)))
        self.assertIsNone(self.notice(self.make_order(expired=False, cheque_deadline_at=None)))
        open_order = self.make_order(expired=False)
        set_hours(0)
        self.assertIsNone(self.notice(open_order))

    def test_the_cheque_form_page_shows_the_countdown_then_hides_it(self):
        order = self.make_order(expired=False)
        page = self.client.get(self.url(order))
        self.assertContains(page, 'data-deadline=')
        self.assertContains(page, 'اطلاعات چک را ثبت کنید')
        self.assertContains(page, '<b class="cheque-deadline-clock"')
        cheques.create_cheque(order, self.user, {'sayadi_id': VALID}, [upload(make_image())])
        page = self.client.get(self.url(order))
        self.assertNotContains(page, 'data-deadline=')

    def test_the_order_detail_page_shows_the_countdown(self):
        from django.urls import reverse
        order = self.make_order(expired=False)
        page = self.client.get(reverse('orders:order_detail_full', args=[order.id]))
        self.assertContains(page, 'data-deadline=')
        self.add_cheque(order)
        page = self.client.get(reverse('orders:order_detail_full', args=[order.id]))
        self.assertNotContains(page, 'data-deadline=')

    def test_an_expired_notice_has_no_clock(self):
        order = self.make_order()
        page = self.client.get(self.url(order))
        self.assertContains(page, 'مهلت ثبت چک به پایان رسیده است')
        self.assertNotContains(page, '<b class="cheque-deadline-clock"')

    def test_the_canceled_order_shows_the_reason_to_the_customer(self):
        from django.urls import reverse
        order = self.make_order()
        self.sweep()
        page = self.client.get(reverse('orders:order_detail_full', args=[order.id]))
        self.assertContains(page, deadline.CANCEL_REASON)


# ------------------------------------------------------------------ پنجره‌ی اصلاح پس از رد (فاز E)
class CorrectionWindowTests(DeadlineBase):
    """ ردِ چک ← پنجره‌ی تازه؛ ارسال مجدد/تأیید ← توقف؛ حذف ← بدون تمدید؛ پایان پنجره ← لغو و آزادسازی رزرو """

    def setUp(self):
        super().setUp()
        from accounts.testing import make_approved_user
        self.admin_user = make_approved_user('09120000070', is_staff=True, is_superuser=True)

    def with_cheque(self, **fields):
        order = self.make_order(expired=False, **fields)
        cheque = cheques.create_cheque(order, self.user, {'sayadi_id': VALID}, [upload(make_image())])
        return order, cheque

    def reject(self, cheque, reason='ناخوانا'):
        return cheques.set_review(cheque, 'rejected', self.admin_user, reason)

    def test_rejecting_the_only_active_cheque_opens_a_fresh_window(self):
        order, cheque = self.with_cheque()
        self.assertIsNone(self.reload(order).cheque_deadline_at)
        before = timezone.now()
        self.reject(cheque)
        fresh = self.reload(order).cheque_deadline_at
        self.assertIsNotNone(fresh)
        self.assertAlmostEqual((fresh - before).total_seconds(), 24 * 3600, delta=30)

    def test_the_window_uses_the_configured_hours(self):
        set_hours(6)
        order, cheque = self.with_cheque()
        self.reject(cheque)
        self.assertAlmostEqual((self.reload(order).cheque_deadline_at - timezone.now()).total_seconds(), 6 * 3600, delta=30)

    def test_a_disabled_setting_opens_no_window(self):
        order, cheque = self.with_cheque()
        set_hours(0)
        self.reject(cheque)
        self.assertIsNone(self.reload(order).cheque_deadline_at)

    def test_rejecting_while_another_cheque_is_active_opens_no_window(self):
        order, cheque = self.with_cheque()
        other = cheques.create_cheque(order, self.user, {'sayadi_id': OTHER}, [upload(make_image())])
        self.reject(other)
        self.assertIsNone(self.reload(order).cheque_deadline_at)

    def test_the_window_opens_only_when_the_last_active_cheque_is_rejected(self):
        order, cheque = self.with_cheque()
        other = cheques.create_cheque(order, self.user, {'sayadi_id': OTHER}, [upload(make_image())])
        cheques.set_review(other, 'approved', self.admin_user)
        self.reject(cheque)                                                       # approved هنوز فعال است
        self.assertIsNone(self.reload(order).cheque_deadline_at)
        self.reject(other)                                                        # حالا هیچ چک فعالی نیست
        self.assertIsNotNone(self.reload(order).cheque_deadline_at)

    def test_the_window_is_fresh_even_if_the_original_deadline_is_long_gone(self):
        order = self.make_order()                                                 # مهلت اولیه گذشته
        cheque = cheques.create_cheque(order, self.user, {'sayadi_id': VALID}, [upload(make_image())])   # ثبت دیرهنگام
        self.reject(cheque)
        self.assertGreater(self.reload(order).cheque_deadline_at, timezone.now() + timedelta(hours=23))
        self.assertEqual(self.sweep(), 0)

    def test_a_legacy_order_without_a_deadline_gets_one_on_rejection(self):
        order = self.make_order(cheque_deadline_at=None)
        Order.objects.filter(pk=order.pk).update(cheque_deadline_at=None)
        cheque = self.add_cheque(order)
        self.reject(cheque)
        self.assertIsNotNone(self.reload(order).cheque_deadline_at)

    def test_resubmitting_the_corrected_cheque_stops_the_clock(self):
        order, cheque = self.with_cheque()
        self.reject(cheque)
        cheques.update_cheque(self.reload(cheque), self.user, {'sayadi_id': VALID}, [])
        self.assertIsNone(self.reload(order).cheque_deadline_at)
        Order.objects.filter(pk=order.pk).update(cheque_deadline_at=self.now - timedelta(hours=1))
        self.assertEqual(self.sweep(), 0)                                         # pending_review فعال است

    def test_approving_stops_the_clock(self):
        order, cheque = self.with_cheque()
        self.reject(cheque)
        cheques.set_review(self.reload(cheque), 'approved', self.admin_user)
        self.assertIsNone(self.reload(order).cheque_deadline_at)

    def test_withdrawing_does_not_extend_the_window(self):
        order, cheque = self.with_cheque()
        self.reject(cheque)
        window = self.reload(order).cheque_deadline_at
        cheques.withdraw_cheque(self.reload(cheque))
        self.assertEqual(self.reload(order).cheque_deadline_at, window)

    def test_an_unanswered_rejection_cancels_the_order_and_releases_the_stock(self):
        order, cheque = self.with_cheque()
        self.assertEqual(self.reserved(), 2)
        self.reject(cheque)
        self.assertEqual(self.sweep(), 0)                                         # پنجره هنوز باز است
        Order.objects.filter(pk=order.pk).update(cheque_deadline_at=self.now - timedelta(minutes=1))
        self.assertEqual(self.sweep(), 1)
        order = self.reload(order)
        self.assertEqual((order.status, order.cancel_reason), ('canceled', deadline.CANCEL_REASON_CORRECTION))
        self.assertEqual(self.reserved(), 0)

    def test_withdrawn_then_unanswered_cancels_too(self):
        order, cheque = self.with_cheque()
        self.reject(cheque)
        cheques.withdraw_cheque(self.reload(cheque))
        Order.objects.filter(pk=order.pk).update(cheque_deadline_at=self.now - timedelta(minutes=1))
        self.assertEqual(self.sweep(), 1)
        self.assertEqual(self.reserved(), 0)

    def test_a_replacement_cheque_inside_the_window_saves_the_order(self):
        order, cheque = self.with_cheque()
        self.reject(cheque)
        cheques.withdraw_cheque(self.reload(cheque))
        cheques.create_cheque(self.reload(order), self.user, {'sayadi_id': OTHER}, [upload(make_image())])
        Order.objects.filter(pk=order.pk).update(cheque_deadline_at=self.now - timedelta(minutes=1))
        self.assertEqual(self.sweep(), 0)                                         # حتی با ساعتِ گذشته: چک فعال دارد

    def test_orders_with_an_active_cheque_do_not_clog_the_batch(self):
        for n in range(3):
            order = self.make_order(qty=1)
            self.add_cheque(order, 'pending_review', sayadi=f'62198610{n + 50:08d}')    # فعال با مهلتِ گذشته‌ی مانده
        victim = self.make_order(qty=1, cheque_deadline_at=self.now - timedelta(hours=5))
        self.assertEqual(self.sweep(limit=1), 1)
        self.assertEqual(self.reload(victim).status, 'canceled')

    def test_the_notice_asks_for_a_correction_and_the_page_shows_it(self):
        order, cheque = self.with_cheque()
        self.reject(cheque)
        info = deadline.deadline_notice(self.reload(order))
        self.assertEqual(info['kind'], 'correct')
        self.assertIn('اصلاح یا چک جایگزین', info['text'])
        page = self.client.get(self.url(order))
        self.assertContains(page, 'data-deadline=')
        self.assertContains(page, 'اصلاح یا چک جایگزین')

    def test_the_notice_is_gone_while_the_corrected_cheque_is_in_review(self):
        order, cheque = self.with_cheque()
        self.reject(cheque)
        cheques.update_cheque(self.reload(cheque), self.user, {'sayadi_id': VALID}, [])
        self.assertIsNone(deadline.deadline_notice(self.reload(order)))

    def test_the_admin_review_form_also_opens_the_window(self):
        from django.test import Client
        from django.urls import reverse
        order, cheque = self.with_cheque()
        admin = Client()
        admin.force_login(self.admin_user)
        admin.post(reverse('admin:orders_chequepayment_change', args=[cheque.pk]), {'status': 'rejected', 'rejection_reason': 'ناخوانا'})
        self.assertEqual(self.reload(cheque).status, 'rejected')
        self.assertIsNotNone(self.reload(order).cheque_deadline_at)
