"""
تست‌های Part B.1: مهلت مرجوعی کالا (SiteSettings.return_period_days/unit، returns/deadline.py،
و انفورس آن در returns/services.py:create_return_request).
"""

from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from orders.models import Order
from products.models import SiteSettings
from returns.deadline import FRIDAY, calculate_return_deadline, is_order_within_return_window
from returns.services import OrderNotDeliveredError, ReturnWindowExpiredError, create_return_request
from returns.tests import ReturnsTestMixin


class CalculateReturnDeadlineTests(TestCase):
    """ تست مستقیم تابع خالص - بدون دیتابیس/سفارش """

    def this_weeks_monday(self, hour=9):
        now = timezone.now()
        monday = now - timedelta(days=now.weekday())
        return monday.replace(hour=hour, minute=0, second=0, microsecond=0)

    def test_calendar_days_just_adds_days_without_touching_the_time(self):
        delivered_at = self.this_weeks_monday(hour=14)
        deadline = calculate_return_deadline(delivered_at, 7, unit='calendar_days')
        self.assertEqual(deadline, delivered_at + timedelta(days=7))

    def test_working_days_from_monday_skips_exactly_one_friday_for_a_7_day_window(self):
        """ دوشنبه + ۷ روز کاری = دقیقاً ۸ روز تقویمی بعد (یک جمعه رد شده) """
        delivered_at = self.this_weeks_monday()
        deadline = calculate_return_deadline(delivered_at, 7, unit='working_days')
        self.assertEqual(deadline.date(), (delivered_at + timedelta(days=8)).date())
        self.assertNotEqual(deadline.weekday(), FRIDAY)

    def test_working_days_deadline_is_end_of_day_not_same_time_as_delivery(self):
        delivered_at = self.this_weeks_monday(hour=14)
        deadline = calculate_return_deadline(delivered_at, 1, unit='working_days')
        self.assertEqual((deadline.hour, deadline.minute, deadline.second), (23, 59, 59))

    def test_thursday_delivery_with_two_working_days_skips_the_friday_in_between(self):
        thursday = self.this_weeks_monday() + timedelta(days=3)
        deadline = calculate_return_deadline(thursday, 2, unit='working_days')
        # پنج‌شنبه(روز۰) +۱=جمعه(رد) +۲=شنبه(کاری۱) +۳=یکشنبه(کاری۲) -> متوقف؛ ۳ روز تقویمی بعد
        self.assertEqual(deadline.date(), (thursday + timedelta(days=3)).date())
        self.assertEqual(deadline.weekday(), 6)   # یکشنبه

    def test_deadline_never_lands_on_a_friday_and_skips_every_friday_in_range(self):
        """ بازآزمایی مستقل: شمارش دستی روزهای غیرجمعه‌ی بین تحویل و مهلت باید دقیقاً برابر days باشد """
        delivered_at = self.this_weeks_monday()
        days = 10
        deadline = calculate_return_deadline(delivered_at, days, unit='working_days')

        self.assertNotEqual(deadline.weekday(), FRIDAY)
        cursor = delivered_at
        working_days_counted = 0
        fridays_skipped = 0
        while cursor.date() < deadline.date():
            cursor += timedelta(days=1)
            if cursor.weekday() == FRIDAY:
                fridays_skipped += 1
            else:
                working_days_counted += 1
        self.assertEqual(working_days_counted, days)
        self.assertGreaterEqual(fridays_skipped, 1)

    def test_unknown_unit_raises(self):
        with self.assertRaises(ValueError):
            calculate_return_deadline(self.this_weeks_monday(), 7, unit='lunar_days')


class IsOrderWithinReturnWindowTests(ReturnsTestMixin, TestCase):
    def setUp(self):
        self.user = self.make_user()
        self.addCleanup(cache.delete, SiteSettings.CACHE_KEY)

    def test_not_delivered_order_is_never_within_window(self):
        order = self.make_order(self.user, status='processing')
        within, reason = is_order_within_return_window(order)
        self.assertFalse(within)
        self.assertTrue(reason)

    def test_delivered_and_within_window_is_true_with_empty_reason(self):
        order = self.make_order(self.user)   # delivered_at خودکار = الان
        within, reason = is_order_within_return_window(order)
        self.assertTrue(within)
        self.assertEqual(reason, '')

    def test_delivered_but_expired_is_false_with_a_dated_reason(self):
        order = self.make_order(self.user)
        Order.objects.filter(pk=order.pk).update(delivered_at=timezone.now() - timedelta(days=30))
        order.refresh_from_db()
        settings_obj = SiteSettings.load()
        settings_obj.return_period_days = 7
        settings_obj.return_period_unit = SiteSettings.RETURN_PERIOD_UNIT_CALENDAR_DAYS
        settings_obj.save()

        within, reason = is_order_within_return_window(order)
        self.assertFalse(within)
        self.assertIn('مهلت', reason)

    def test_explicit_now_parameter_is_respected(self):
        order = self.make_order(self.user)
        settings_obj = SiteSettings.load()
        settings_obj.return_period_days = 7
        settings_obj.return_period_unit = SiteSettings.RETURN_PERIOD_UNIT_CALENDAR_DAYS
        settings_obj.save()

        far_future = order.delivered_at + timedelta(days=100)
        within, reason = is_order_within_return_window(order, now=far_future)
        self.assertFalse(within)


class CreateReturnRequestDeadlineGuardTests(ReturnsTestMixin, TestCase):
    def setUp(self):
        self.user = self.make_user()
        self.category = self.make_category()
        self.product = self.make_product(self.category)
        self.reason = self.make_reason()
        self.addCleanup(cache.delete, SiteSettings.CACHE_KEY)

    def _order_item(self, order, quantity=1):
        return self.make_order_item(order, self.product, price=10000, quantity=quantity)

    def _attempt(self, order, order_item):
        return create_return_request(
            order, self.user,
            [{'order_item': order_item, 'reason': self.reason, 'requested_quantity': 1}],
            refund_method='wallet',
        )

    def test_non_delivered_order_is_blocked(self):
        order = self.make_order(self.user, status='processing')
        item = self._order_item(order)
        with self.assertRaises(OrderNotDeliveredError):
            self._attempt(order, item)

    def test_delivered_order_without_delivered_at_is_blocked(self):
        """ حالت غیرمنتظره‌ی داده‌ای (delivered_at خالی روی سفارش delivered) هم باید مسدود شود """
        order = self.make_order(self.user)
        item = self._order_item(order)
        Order.objects.filter(pk=order.pk).update(delivered_at=None)
        order.refresh_from_db()
        with self.assertRaises(OrderNotDeliveredError):
            self._attempt(order, item)

    def test_delivered_order_past_calendar_deadline_is_blocked(self):
        """ ۸ روز بعد از تحویل، در مهلت تقویمی ۷روزه - باید مسدود شود """
        settings_obj = SiteSettings.load()
        settings_obj.return_period_days = 7
        settings_obj.return_period_unit = SiteSettings.RETURN_PERIOD_UNIT_CALENDAR_DAYS
        settings_obj.save()

        order = self.make_order(self.user)
        item = self._order_item(order)
        Order.objects.filter(pk=order.pk).update(delivered_at=timezone.now() - timedelta(days=8))
        order.refresh_from_db()

        with self.assertRaises(ReturnWindowExpiredError) as ctx:
            self._attempt(order, item)
        self.assertIn('7', str(ctx.exception))   # پیام شامل عدد مهلت باشد
        self.assertIn('مهلت', str(ctx.exception))

    def test_delivered_order_within_calendar_deadline_succeeds(self):
        settings_obj = SiteSettings.load()
        settings_obj.return_period_days = 7
        settings_obj.return_period_unit = SiteSettings.RETURN_PERIOD_UNIT_CALENDAR_DAYS
        settings_obj.save()

        order = self.make_order(self.user)
        item = self._order_item(order)
        Order.objects.filter(pk=order.pk).update(delivered_at=timezone.now() - timedelta(days=6))
        order.refresh_from_db()

        request = self._attempt(order, item)
        self.assertIsNotNone(request.pk)

    def test_boundary_exact_deadline_instant_still_succeeds(self):
        settings_obj = SiteSettings.load()
        settings_obj.return_period_days = 7
        settings_obj.return_period_unit = SiteSettings.RETURN_PERIOD_UNIT_CALENDAR_DAYS
        settings_obj.save()

        order = self.make_order(self.user)
        item = self._order_item(order)
        deadline = calculate_return_deadline(order.delivered_at, 7, unit='calendar_days')

        with mock.patch('returns.services.timezone.now', return_value=deadline):
            request = self._attempt(order, item)
        self.assertIsNotNone(request.pk)

    def test_boundary_one_microsecond_after_deadline_fails(self):
        settings_obj = SiteSettings.load()
        settings_obj.return_period_days = 7
        settings_obj.return_period_unit = SiteSettings.RETURN_PERIOD_UNIT_CALENDAR_DAYS
        settings_obj.save()

        order = self.make_order(self.user)
        item = self._order_item(order)
        deadline = calculate_return_deadline(order.delivered_at, 7, unit='calendar_days')

        with mock.patch('returns.services.timezone.now', return_value=deadline + timedelta(microseconds=1)):
            with self.assertRaises(ReturnWindowExpiredError):
                self._attempt(order, item)

    def test_working_days_deadline_survives_a_friday_in_between(self):
        """ سفارشی که با مهلت ۲ روزکاری، تحویلش پنج‌شنبه بوده - جمعه‌ی بین راه نباید مهلت را ببلعد """
        settings_obj = SiteSettings.load()
        settings_obj.return_period_days = 2
        settings_obj.return_period_unit = SiteSettings.RETURN_PERIOD_UNIT_WORKING_DAYS
        settings_obj.save()

        now = timezone.now()
        thursday = (now - timedelta(days=now.weekday())) + timedelta(days=3)
        thursday = thursday.replace(hour=9, minute=0, second=0, microsecond=0)

        order = self.make_order(self.user)
        item = self._order_item(order)
        Order.objects.filter(pk=order.pk).update(delivered_at=thursday)
        order.refresh_from_db()

        deadline = calculate_return_deadline(thursday, 2, unit='working_days')
        with mock.patch('returns.services.timezone.now', return_value=deadline - timedelta(minutes=1)):
            request = self._attempt(order, item)
        self.assertIsNotNone(request.pk)

    def test_changing_site_settings_return_period_has_immediate_effect(self):
        """
        همان سفارش، همان delivered_at؛ فقط تنظیمات سایت عوض می‌شود و نتیجه‌ی اعتبارسنجی بدون
        هیچ کش/دیپلوی مجددی بلافاصله برعکس می‌شود.
        """
        settings_obj = SiteSettings.load()
        settings_obj.return_period_days = 7
        settings_obj.return_period_unit = SiteSettings.RETURN_PERIOD_UNIT_CALENDAR_DAYS
        settings_obj.save()

        order = self.make_order(self.user)
        item = self._order_item(order, quantity=2)
        Order.objects.filter(pk=order.pk).update(delivered_at=timezone.now() - timedelta(days=10))
        order.refresh_from_db()

        with self.assertRaises(ReturnWindowExpiredError):
            self._attempt(order, item)   # ۱۰ روز > ۷ روز مهلت -> رد

        settings_obj.return_period_days = 14
        settings_obj.save()

        request = self._attempt(order, item)   # همان ۱۰ روز، حالا < ۱۴ روز مهلت -> موفق
        self.assertIsNotNone(request.pk)
