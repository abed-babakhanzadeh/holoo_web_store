"""
تنظیمات گفتگوی آنلاین در SiteSettings (فاز ۱): منطق خالص products/chat_settings.py، پیش‌فرض‌ها، اعتبارسنجی فرم ادمین.
"""
from datetime import datetime, timezone as dt_timezone
from zoneinfo import ZoneInfo

import jdatetime
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from products import chat_settings as cs
from products.models import SiteSettings

TEHRAN = ZoneInfo('Asia/Tehran')


class HoursParsingTests(SimpleTestCase):
    def test_single_and_multiple_ranges(self):
        self.assertEqual(cs.parse_hours_line('08:00-12:00'), [(480, 720)])
        self.assertEqual(cs.parse_hours_line('13:00-17:00, 08:00-12:00'), [(480, 720), (780, 1020)])

    def test_persian_digits_and_separators_are_accepted(self):
        self.assertEqual(cs.parse_hours_line('۰۸:۰۰-۱۲:۰۰، ۱۳:۰۰–۱۷:۰۰'), [(480, 720), (780, 1020)])

    def test_blank_means_closed(self):
        self.assertEqual(cs.parse_hours_line(''), [])
        self.assertEqual(cs.parse_hours_line('   '), [])

    def test_invalid_formats_are_rejected_with_a_persian_message(self):
        for bad in ('8-12', '08:00 12:00', 'صبح تا ظهر', '08:00-', '25:00-26:00', '08:61-09:00'):
            with self.subTest(bad=bad), self.assertRaises(ValueError) as caught:
                cs.parse_hours_line(bad)
            self.assertTrue(str(caught.exception))

    def test_start_must_precede_end_and_ranges_must_not_overlap(self):
        for bad in ('12:00-08:00', '08:00-08:00', '22:00-02:00', '08:00-12:00, 11:00-13:00'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cs.parse_hours_line(bad)

    def test_adjacent_ranges_are_allowed(self):
        self.assertEqual(cs.parse_hours_line('08:00-12:00, 12:00-13:00'), [(480, 720), (720, 780)])

    def test_label_is_persian(self):
        self.assertEqual(cs.format_ranges([(480, 720), (780, 1020)]), '۰۸:۰۰ تا ۱۲:۰۰ و ۱۳:۰۰ تا ۱۷:۰۰')


class HolidayAndTimezoneTests(SimpleTestCase):
    def test_jalali_dates_become_gregorian(self):
        parsed = cs.parse_holidays('1405/07/21 عید فرضی\n# توضیح\n\n۱۴۰۵/۰۷/۲۲')
        self.assertEqual(parsed[jdatetime.date(1405, 7, 21).togregorian()], 'عید فرضی')
        self.assertEqual(parsed[jdatetime.date(1405, 7, 22).togregorian()], '')
        self.assertEqual(len(parsed), 2)

    def test_bad_lines_report_the_line_number(self):
        with self.assertRaisesMessage(ValueError, 'خط 2'):
            cs.parse_holidays('1405/07/21\nسلام')
        with self.assertRaisesMessage(ValueError, 'خط 1'):
            cs.parse_holidays('1405/13/45')

    def test_timezone_validation(self):
        cs.validate_timezone('Asia/Tehran')
        for bad in ('', 'Mars/Phobos', 'Asia'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cs.validate_timezone(bad)


class WorkingStateTests(SimpleTestCase):
    """ شنبه ۱۴۰۵/۰۷/۱۹ (۱۱ اکتبر ۲۰۲۶ = یکشنبه میلادی)؛ برنامه را از روی روز هفته‌ی همان تاریخ می‌سازیم """
    DAY = datetime(2026, 10, 10, 10, 0, tzinfo=TEHRAN)         # مبنای محاسبه

    def schedule(self, ranges):
        return {weekday: (ranges if weekday == self.DAY.weekday() else []) for weekday in range(7)}

    def state(self, hour, minute=0, *, ranges='08:00-12:00, 13:00-17:00', holidays=None, mode='by_schedule', tz='Asia/Tehran'):
        schedule = self.schedule(cs.parse_hours_line(ranges))
        return cs.working_state(mode, schedule, holidays or {}, tz, self.DAY.replace(hour=hour, minute=minute))

    def test_inside_a_range(self):
        result = self.state(9, 30)
        self.assertTrue(result['in_hours'])
        self.assertIn('۰۸:۰۰ تا ۱۲:۰۰', result['today_label'])

    def test_range_start_is_inclusive_and_end_is_exclusive(self):
        self.assertTrue(self.state(8, 0)['in_hours'])
        self.assertFalse(self.state(12, 0)['in_hours'])
        self.assertTrue(self.state(13, 0)['in_hours'])
        self.assertFalse(self.state(17, 0)['in_hours'])

    def test_lunch_gap_points_to_the_afternoon_opening(self):
        result = self.state(12, 30)
        self.assertFalse(result['in_hours'])
        self.assertEqual(result['next_open_label'], 'امروز ساعت ۱۳:۰۰')

    def test_after_closing_points_to_the_next_working_day(self):
        result = self.state(18)
        self.assertFalse(result['in_hours'])
        self.assertTrue(result['next_open_label'].endswith('۰۸:۰۰'))
        self.assertNotIn('امروز', result['next_open_label'])

    def test_closed_day_and_holiday(self):
        closed = self.state(10, ranges='')
        self.assertFalse(closed['in_hours'])
        self.assertIn('تعطیل', closed['today_label'])
        holiday = self.state(10, holidays={self.DAY.date(): 'تعطیل رسمی'})
        self.assertFalse(holiday['in_hours'])
        self.assertIn('تعطیل رسمی', holiday['today_label'])

    def test_always_mode_ignores_everything(self):
        self.assertTrue(self.state(3, ranges='', mode='always', holidays={self.DAY.date(): 'x'})['in_hours'])

    def test_the_configured_timezone_decides_not_the_server_clock(self):
        instant = datetime(2026, 10, 10, 6, 0, tzinfo=dt_timezone.utc)                # ۰۹:۳۰ تهران، ۰۶:۰۰ UTC
        schedule = self.schedule(cs.parse_hours_line('08:00-12:00'))
        self.assertTrue(cs.working_state('by_schedule', schedule, {}, 'Asia/Tehran', instant)['in_hours'])
        self.assertFalse(cs.working_state('by_schedule', schedule, {}, 'UTC', instant)['in_hours'])

    def test_midnight_in_the_zone_changes_the_day(self):
        schedule = {w: cs.parse_hours_line('00:00-23:59') for w in range(7)}
        holidays = {datetime(2026, 10, 11).date(): 'تعطیل'}
        before = datetime(2026, 10, 10, 20, 0, tzinfo=dt_timezone.utc)                 # ۲۳:۳۰ تهران، ۱۰ اکتبر
        after = datetime(2026, 10, 10, 20, 45, tzinfo=dt_timezone.utc)                  # ۰۰:۱۵ تهران، ۱۱ اکتبر (تعطیل)
        self.assertTrue(cs.working_state('by_schedule', schedule, holidays, 'Asia/Tehran', before)['in_hours'])
        self.assertFalse(cs.working_state('by_schedule', schedule, holidays, 'Asia/Tehran', after)['in_hours'])
        self.assertTrue(cs.working_state('by_schedule', schedule, holidays, 'UTC', after)['in_hours'])      # در UTC هنوز ۱۰ اکتبر


class ExcludedPathsTests(SimpleTestCase):
    def test_exact_prefix_and_name_patterns(self):
        patterns = cs.parse_excluded_paths('/orders/checkout/\n/payments/*\nname:accounts:dashboard\n# یادداشت\n')
        self.assertEqual(patterns, [('exact', '/orders/checkout/'), ('prefix', '/payments/'), ('name', 'accounts:dashboard')])
        self.assertTrue(cs.path_is_excluded(patterns, '/orders/checkout/'))
        self.assertTrue(cs.path_is_excluded(patterns, '/orders/checkout'))             # با/بی «/» پایانی
        self.assertFalse(cs.path_is_excluded(patterns, '/orders/checkout/done/'))
        self.assertTrue(cs.path_is_excluded(patterns, '/payments/verify/123/'))
        self.assertTrue(cs.path_is_excluded(patterns, '/accounts/dashboard/', 'accounts:dashboard'))
        self.assertFalse(cs.path_is_excluded(patterns, '/accounts/profile/', 'accounts:profile'))
        self.assertFalse(cs.path_is_excluded([], '/anything/'))

    def test_a_prefix_without_the_trailing_slash_matches_the_whole_family(self):
        patterns = cs.parse_excluded_paths('/pay*')
        self.assertTrue(cs.path_is_excluded(patterns, '/payments/x'))

    def test_invalid_patterns_are_rejected(self):
        for bad in ('orders/checkout/', '/a*b/', '/a b/', '/a?x=1', 'name:bad name', 'name:'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cs.parse_excluded_paths(bad)

    def test_regex_is_never_interpreted(self):
        # الگو فقط یک مسیر حرفی است (موتور regex وجود ندارد؛ پس ReDoS ممکن نیست)
        patterns = cs.parse_excluded_paths('/(a+)+$/')
        self.assertEqual(patterns, [('exact', '/(a+)+$/')])
        self.assertFalse(cs.path_is_excluded(patterns, '/aaaa/'))
        self.assertTrue(cs.path_is_excluded(patterns, '/(a+)+$/'))

    def test_the_line_cap(self):
        cs.parse_excluded_paths('\n'.join(f'/p{i}/' for i in range(cs.MAX_EXCLUDED_LINES)))
        with self.assertRaises(ValueError):
            cs.parse_excluded_paths('\n'.join(f'/p{i}/' for i in range(cs.MAX_EXCLUDED_LINES + 1)))


class MiscParsingTests(SimpleTestCase):
    def test_bubble_lines(self):
        self.assertEqual(cs.parse_bubble_lines('سلام\n\n  کمک؟  \n'), ['سلام', 'کمک؟'])
        with self.assertRaises(ValueError):
            cs.parse_bubble_lines('x' * (cs.MAX_BUBBLE_LENGTH + 1))
        with self.assertRaises(ValueError):
            cs.parse_bubble_lines('\n'.join('a' for _ in range(cs.MAX_BUBBLE_LINES + 1)))

    def test_phone_list(self):
        self.assertEqual(cs.parse_phone_list('09121234567\n۰۹۱۲۱۲۳۴۵۶۸\n'), ['09121234567', '09121234568'])
        with self.assertRaises(ValueError):
            cs.parse_phone_list('0912')

    def test_clamp(self):
        self.assertEqual(cs.clamp(5, 1, 10), 5)
        self.assertEqual(cs.clamp(-3, 1, 10), 1)
        self.assertEqual(cs.clamp(99, 1, 10), 10)
        self.assertEqual(cs.clamp('x', 2, 10), 2)
        self.assertEqual(cs.clamp(None, 2, 10, default=7), 7)

    def test_persian_digits(self):
        self.assertEqual(cs.to_persian_digits('ساعت 08:30'), 'ساعت ۰۸:۳۰')


class DefaultsAndValidationTests(TestCase):
    def setUp(self):
        self.s = SiteSettings.load()

    def test_chat_is_off_by_default_and_ai_tab_shows_coming_soon(self):
        self.assertFalse(self.s.chat_enabled)
        self.assertEqual(self.s.chat_ai_tab_mode, 'coming_soon')
        self.assertTrue(self.s.chat_tab_live_enabled and self.s.chat_tab_offline_enabled)
        self.assertFalse(self.s.chat_attachments_enabled)
        self.assertEqual(self.s.chat_timezone, 'Asia/Tehran')
        self.assertTrue(self.s.chat_respect_reduced_motion)
        self.assertIn('/orders/checkout/*', self.s.chat_excluded_paths)
        self.assertIn('/payments/*', self.s.chat_excluded_paths)

    def test_default_settings_pass_validation(self):
        self.assertEqual(cs.validate_chat_settings(self.s), {})
        self.s.clean()

    def test_each_invalid_field_is_reported_on_its_own_field(self):
        cases = {
            'chat_timezone': 'Nowhere/Land', 'chat_excluded_paths': 'checkout', 'chat_bubble_messages': 'x' * 200,
            'chat_holidays': 'امروز', 'chat_notify_phones': '123', 'chat_hours_sat': '9-5', 'chat_hours_fri': '10:00-09:00',
            'chat_primary_color': 'orange',
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                self.s = SiteSettings.load()
                setattr(self.s, field, value)
                self.assertIn(field, cs.validate_chat_settings(self.s))
                with self.assertRaises(ValidationError) as caught:
                    self.s.clean()
                self.assertIn(field, caught.exception.message_dict)

    def test_poll_and_timer_relationships(self):
        self.s.chat_poll_closed_seconds = 5
        self.s.chat_poll_idle_seconds, self.s.chat_poll_active_seconds = 3, 5
        self.s.chat_customer_gone_minutes, self.s.chat_customer_idle_minutes = 10, 10
        errors = cs.validate_chat_settings(self.s)
        for field in ('chat_poll_closed_seconds', 'chat_poll_idle_seconds', 'chat_customer_gone_minutes'):
            self.assertIn(field, errors)
        self.s.chat_poll_closed_seconds = 0                          # ۰ = بدون پولینگ مجاز است
        self.assertNotIn('chat_poll_closed_seconds', cs.validate_chat_settings(self.s))

    def test_schedule_mode_needs_at_least_one_working_day(self):
        for _, key, _name in cs.WEEK:
            setattr(self.s, f'chat_hours_{key}', '')
        self.assertIn('chat_hours_mode', cs.validate_chat_settings(self.s))
        self.s.chat_hours_mode = 'always'
        self.assertEqual(cs.validate_chat_settings(self.s), {})

    def test_state_helpers_survive_corrupt_values_written_straight_to_the_database(self):
        self.s.chat_hours_sat = 'خراب'
        self.s.chat_holidays = 'خراب'
        self.s.chat_timezone = 'Nowhere/Land'
        state = cs.current_hours_state(self.s, datetime(2026, 10, 10, 10, 0, tzinfo=TEHRAN))
        self.assertIn('in_hours', state)                                      # خطا نمی‌دهد؛ روز خراب تعطیل حساب می‌شود
        self.s.chat_excluded_paths = 'خراب'
        self.s.chat_enabled = True
        self.assertTrue(cs.widget_should_render(self.s, is_authenticated=False, path='/shop/'))   # الگوی خراب ویجت را نمی‌شکند


class WidgetDecisionTests(TestCase):
    def setUp(self):
        self.s = SiteSettings.load()
        self.s.chat_enabled = True

    def render(self, *, auth=False, path='/shop/', name=''):
        return cs.widget_should_render(self.s, is_authenticated=auth, path=path, url_name=name)

    def test_master_switch(self):
        self.assertTrue(self.render())
        self.s.chat_enabled = False
        self.assertFalse(self.render())

    def test_audience(self):
        self.s.chat_visible_for_guests, self.s.chat_visible_for_users = False, True
        self.assertFalse(self.render(auth=False))
        self.assertTrue(self.render(auth=True))
        self.s.chat_visible_for_guests, self.s.chat_visible_for_users = True, False
        self.assertTrue(self.render(auth=False))
        self.assertFalse(self.render(auth=True))

    def test_no_visible_tab_means_no_widget(self):
        self.s.chat_tab_live_enabled = self.s.chat_tab_offline_enabled = False
        self.s.chat_ai_tab_mode = 'hidden'
        self.assertFalse(self.render())
        self.s.chat_ai_tab_mode = 'coming_soon'
        self.assertTrue(self.render())        # فقط زبانه‌ی «به‌زودی» هم یک زبانه است

    def test_excluded_paths_by_path_prefix_and_url_name(self):
        self.assertFalse(self.render(path='/orders/checkout/'))
        self.assertFalse(self.render(path='/payments/zarinpal/verify/'))
        self.s.chat_excluded_paths = 'name:products:product_list'
        self.assertFalse(self.render(path='/shop/', name='products:product_list'))
        self.assertTrue(self.render(path='/shop/other/', name='products:home'))

    def test_visible_tabs_follow_the_switches(self):
        keys = lambda: [t[0] for t in cs.visible_tabs(self.s)]
        self.assertEqual(keys(), ['live', 'offline', 'ai'])
        self.s.chat_tab_live_enabled = False
        self.assertEqual(keys(), ['offline', 'ai'])
        self.s.chat_ai_tab_mode = 'hidden'
        self.assertEqual(keys(), ['offline'])
