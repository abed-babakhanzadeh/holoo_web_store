"""
گفتگوی آنلاین، فاز ۱: GET /chat/config/، تصمیم رندر در قالب (کلید اصلی، مخاطب، صفحات مستثنی)، و دارایی‌های استاتیک.
هیچ تستی در media/ واقعی نمی‌نویسد (آواتار اختصاصی در MEDIA_ROOT موقت).
"""
import json
import shutil
import subprocess
import tempfile
import xml.dom.minidom
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import CustomUser
from chat.services import CHAT_BACKEND_READY, public_config
from products.chat_settings import CHAT_CFG_CACHE_KEY
from products.models import SiteSettings

BASE = Path(settings.BASE_DIR)
TEHRAN = ZoneInfo('Asia/Tehran')
# ۱۰ اکتبر ۲۰۲۶ ساعت ۱۰:۰۰ تهران و ۱۸:۰۰ تهران؛ برنامه را از روز هفته‌ی همان تاریخ می‌سازیم تا به تقویم وابسته نباشد
IN_HOURS = datetime(2026, 10, 10, 10, 0, tzinfo=TEHRAN)
AFTER_HOURS = datetime(2026, 10, 10, 18, 0, tzinfo=TEHRAN)


class ChatTestBase(TestCase):
    def setUp(self):
        self.addCleanup(cache.delete, SiteSettings.CACHE_KEY)
        self.addCleanup(cache.delete, CHAT_CFG_CACHE_KEY)
        cache.delete(CHAT_CFG_CACHE_KEY)

    def set(self, **values):
        SiteSettings.objects.update_or_create(pk=1, defaults=values)
        cache.delete(SiteSettings.CACHE_KEY)

    def enable(self, **values):
        self.set(chat_enabled=True, **values)

    def config(self, client=None):
        response = (client or self.client).get(reverse('chat:config'))
        self.assertEqual(response.status_code, 200)
        return response, json.loads(response.content)


class ConfigEndpointTests(ChatTestBase):
    def test_disabled_chat_reveals_nothing(self):
        _, data = self.config()
        self.assertEqual(data, {'enabled': False})

    def test_enabled_payload_has_the_contract_the_client_relies_on(self):
        self.enable()
        response, data = self.config()
        self.assertTrue(data['enabled'])
        for key in ('backend_ready', 'color', 'position', 'tabs', 'texts', 'avatar', 'anim', 'bubble', 'dismiss_hours',
                    'guest_form', 'limits', 'hours', 'availability', 'viewer'):
            self.assertIn(key, data)
        self.assertEqual(data['backend_ready'], CHAT_BACKEND_READY)
        self.assertEqual(data['viewer'], {'authenticated': False})
        self.assertEqual(response['Cache-Control'], 'private, max-age=30')
        self.assertIn('Cookie', response['Vary'])

    def test_no_internal_or_future_phase_settings_leak_to_the_client(self):
        self.enable(chat_notify_phones='09121234567', chat_operator_response_sla_minutes=7)
        _, data = self.config()
        text = json.dumps(data, ensure_ascii=False)
        self.assertNotIn('09121234567', text)
        for forbidden in ('sla', 'notify', 'retention', 'sms', 'assignee'):
            self.assertNotIn(forbidden, ' '.join(data.keys()).lower())

    def test_tabs_follow_the_switches_and_the_ai_tab_modes(self):
        self.enable()
        self.assertEqual([t['key'] for t in self.config()[1]['tabs']], ['live', 'offline', 'ai'])
        self.assertTrue(self.config()[1]['tabs'][2]['coming_soon'])
        self.set(chat_tab_live_enabled=False)
        self.assertEqual([t['key'] for t in self.config()[1]['tabs']], ['offline', 'ai'])
        self.set(chat_ai_tab_mode='hidden')
        self.assertEqual([t['key'] for t in self.config()[1]['tabs']], ['offline'])
        self.set(chat_tab_offline_enabled=False)
        self.assertEqual(self.config()[1], {'enabled': False})                 # هیچ زبانه‌ای نمانده

    def test_audience_gate(self):
        self.enable(chat_visible_for_guests=False)
        self.assertEqual(self.config()[1], {'enabled': False})
        user = CustomUser.objects.create_user('09125551001')
        self.client.force_login(user)
        _, data = self.config()
        self.assertTrue(data['enabled'])
        self.assertTrue(data['viewer']['authenticated'])
        self.set(chat_visible_for_users=False)
        self.assertEqual(self.config()[1], {'enabled': False})

    def test_texts_and_labels_come_from_the_admin_settings(self):
        self.enable(chat_title='پشتیبانی تست', chat_tab_offline_label='نامه', chat_welcome_message='درود', chat_msg_after_hours='بسته‌ایم')
        _, data = self.config()
        self.assertEqual(data['texts']['title'], 'پشتیبانی تست')
        self.assertEqual(data['texts']['welcome'], 'درود')
        self.assertEqual(data['texts']['after_hours'], 'بسته‌ایم')
        self.assertEqual([t['label'] for t in data['tabs'] if t['key'] == 'offline'], ['نامه'])

    def test_position_animation_bubble_and_guest_form_settings_are_passed_through_clamped(self):
        self.enable(chat_position='right', chat_offset_x_px=33, chat_offset_y_px=44, chat_offset_y_px_mobile=None,
                    chat_anim_enabled=False, chat_anim_wave=False, chat_bubble_messages='یک\nدو', chat_bubble_interval_seconds=12,
                    chat_attention_interval_seconds=60, chat_guest_name_mode='required', chat_guest_phone_mode='hidden',
                    chat_message_max_length=500, chat_launcher_dismiss_hours=6, chat_primary_color='#112233')
        _, data = self.config()
        self.assertEqual(data['position'], {'side': 'right', 'x': 33, 'y': 44, 'x_mobile': None, 'y_mobile': None})
        self.assertFalse(data['anim']['enabled'])
        self.assertFalse(data['anim']['wave'])
        self.assertTrue(data['anim']['float'])
        self.assertEqual(data['anim']['attention_interval'], 60)
        self.assertEqual(data['bubble'], {'messages': ['یک', 'دو'], 'interval': 12, 'first_delay': 4})
        self.assertEqual(data['guest_form'], {'name': 'required', 'phone': 'hidden'})
        self.assertEqual(data['limits']['message_max_length'], 500)
        self.assertEqual(data['dismiss_hours'], 6)
        self.assertEqual(data['color'], '#112233')

    def test_out_of_range_values_written_straight_to_the_database_are_clamped(self):
        self.enable(chat_offset_x_px=9999, chat_message_max_length=30000, chat_bubble_interval_seconds=0,
                    chat_attention_interval_seconds=1)
        _, data = self.config()
        self.assertEqual(data['position']['x'], 200)
        self.assertEqual(data['limits']['message_max_length'], 4000)
        self.assertEqual(data['bubble']['interval'], 3)
        self.assertEqual(data['anim']['attention_interval'], 5)

    def test_an_empty_or_corrupt_bubble_list_just_disables_the_bubble(self):
        self.enable(chat_bubble_messages='')
        self.assertEqual(self.config()[1]['bubble']['messages'], [])
        self.set(chat_bubble_messages='x' * 500)
        self.assertEqual(self.config()[1]['bubble']['messages'], [])

    def test_only_get_is_allowed(self):
        self.enable()
        self.assertEqual(self.client.post(reverse('chat:config')).status_code, 405)

    def test_the_snapshot_is_cached_and_invalidated_when_settings_are_saved(self):
        self.enable(chat_title='قبل')
        self.config()
        self.assertIsNotNone(cache.get(CHAT_CFG_CACHE_KEY))
        s = SiteSettings.load()
        s.chat_title = 'بعد'
        s.save()                                                              # post_save ← باطل‌سازی
        self.assertIsNone(cache.get(CHAT_CFG_CACHE_KEY))
        self.assertEqual(self.config()[1]['texts']['title'], 'بعد')

    def test_a_redis_outage_does_not_break_the_config(self):
        from unittest import mock
        self.enable()
        with mock.patch('chat.services.cache.get', side_effect=OSError('redis down')), \
                mock.patch('chat.services.cache.set', side_effect=OSError('redis down')):
            data = public_config(SiteSettings.load(), is_authenticated=False)
        self.assertTrue(data['enabled'])


class AvailabilityTests(ChatTestBase):
    """ فاز ۱: کارشناس آنلاینی وجود ندارد ← گفتگوی زنده همیشه ناموجود؛ فقط علتش (خارج ساعت/بدون کارشناس) فرق می‌کند """

    def configure_hours(self):
        weekday = IN_HOURS.weekday()
        fields = {f'chat_hours_{key}': '' for _, key, _name in __import__('products.chat_settings', fromlist=['WEEK']).WEEK}
        key = {w: k for w, k, _ in __import__('products.chat_settings', fromlist=['WEEK']).WEEK}[weekday]
        fields[f'chat_hours_{key}'] = '08:00-12:00, 13:00-17:00'
        self.enable(chat_hours_mode='by_schedule', chat_holidays='', **fields)

    def test_in_hours_without_operator_is_state_two(self):
        self.configure_hours()
        data = public_config(SiteSettings.load(), is_authenticated=False, now=IN_HOURS)
        self.assertEqual(data['availability'], {'live': False, 'state': 'no_operator'})
        self.assertTrue(data['hours']['in_hours'])

    def test_after_hours_is_state_three_with_the_next_opening(self):
        self.configure_hours()
        data = public_config(SiteSettings.load(), is_authenticated=False, now=AFTER_HOURS)
        self.assertEqual(data['availability'], {'live': False, 'state': 'after_hours'})
        self.assertFalse(data['hours']['in_hours'])
        self.assertTrue(data['hours']['next_open_label'])

    def test_a_holiday_is_after_hours_whatever_the_schedule(self):
        self.configure_hours()
        s = SiteSettings.load()
        import jdatetime
        jalali = jdatetime.date.fromgregorian(date=IN_HOURS.date())
        s.chat_holidays = f'{jalali.year}/{jalali.month}/{jalali.day} روز تست'
        data = public_config(s, is_authenticated=False, now=IN_HOURS)
        self.assertEqual(data['availability']['state'], 'after_hours')
        self.assertIn('روز تست', data['hours']['today_label'])

    def test_always_mode(self):
        self.enable(chat_hours_mode='always')
        data = public_config(SiteSettings.load(), is_authenticated=False, now=AFTER_HOURS)
        self.assertTrue(data['hours']['in_hours'])
        self.assertEqual(data['availability']['state'], 'no_operator')


class AvatarTests(ChatTestBase):
    def test_built_in_avatars(self):
        self.enable(chat_avatar_choice='support')
        self.assertEqual(self.config()[1]['avatar']['kind'], 'support')
        self.assertTrue(self.config()[1]['avatar']['url'].endswith('chat/avatar-support.svg'))
        self.set(chat_avatar_choice='character')
        self.assertTrue(self.config()[1]['avatar']['url'].endswith('chat/avatar-character.svg'))

    def test_custom_choice_without_a_file_falls_back_to_the_default(self):
        self.enable(chat_avatar_choice='custom')
        self.assertEqual(self.config()[1]['avatar']['kind'], 'support')

    def test_custom_upload_is_served_from_media_and_never_touches_the_real_media_root(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with override_settings(MEDIA_ROOT=tmp):
            s = SiteSettings.load()
            s.chat_enabled = True
            s.chat_avatar_choice = 'custom'
            s.chat_avatar_custom = SimpleUploadedFile('me.png', b'\x89PNG\r\n\x1a\n' + b'0' * 20, content_type='image/png')
            s.save()
            cache.delete(SiteSettings.CACHE_KEY)
            avatar = self.config()[1]['avatar']
            self.assertEqual(avatar['kind'], 'custom')
            self.assertTrue(avatar['url'].startswith(settings.MEDIA_URL + 'chat/avatar/'))
            self.assertTrue(any(Path(tmp, 'chat', 'avatar').iterdir()))
        self.assertFalse((Path(settings.MEDIA_ROOT) / 'chat' / 'avatar' / 'me.png').exists())


class TemplateRenderTests(ChatTestBase):
    def page(self, path='/shop/', client=None):
        response = (client or self.client).get(path)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_nothing_is_rendered_while_chat_is_off(self):
        html = self.page()
        self.assertNotIn('id="chat-root"', html)
        self.assertNotIn('chat-widget.js', html)
        self.assertNotIn('chat-widget.css', html)

    def test_root_script_and_style_are_rendered_once_when_enabled(self):
        self.enable()
        html = self.page()
        self.assertEqual(html.count('id="chat-root"'), 1)
        self.assertEqual(html.count('chat-widget.js'), 1)
        self.assertEqual(html.count('chat-widget.css'), 1)
        self.assertIn(f'data-config-url="{reverse("chat:config")}"', html)
        self.assertIn('id="mobile-bottom-nav"', html)

    def test_excluded_paths_load_nothing_at_all(self):
        self.enable(chat_excluded_paths='/shop/*')
        html = self.page()
        self.assertNotIn('chat-widget', html)
        self.assertNotIn('chat-root', html)
        self.set(chat_excluded_paths='name:products:product_list')
        self.assertNotIn('chat-root', self.page())
        self.set(chat_excluded_paths='/somewhere-else/')
        self.assertIn('chat-root', self.page())

    def test_audience_decides_rendering(self):
        self.enable(chat_visible_for_guests=False)
        self.assertNotIn('chat-root', self.page())
        user = CustomUser.objects.create_user('09125551002')
        self.client.force_login(user)
        self.assertIn('chat-root', self.page())

    def test_the_checkout_and_payment_defaults_are_excluded(self):
        from products.chat_settings import parse_excluded_paths, path_is_excluded
        patterns = parse_excluded_paths(SiteSettings.load().chat_excluded_paths)
        self.assertTrue(path_is_excluded(patterns, reverse('orders:checkout')))
        self.assertTrue(path_is_excluded(patterns, '/payments/anything/'))


class StaticAssetTests(TestCase):
    def test_avatars_are_wellformed_svg_with_the_hooks_the_stylesheet_animates(self):
        support = BASE / 'static/theme/assets/chat/avatar-support.svg'
        character = BASE / 'static/theme/assets/chat/avatar-character.svg'
        for path in (support, character):
            with self.subTest(path=path.name):
                dom = xml.dom.minidom.parse(str(path))
                self.assertEqual(dom.documentElement.tagName, 'svg')
                self.assertTrue(dom.documentElement.getAttribute('viewBox'))
                self.assertLess(path.stat().st_size, 6000)
                self.assertNotIn('<script', path.read_text(encoding='utf-8'))
                self.assertNotIn('http://', path.read_text(encoding='utf-8').replace('http://www.w3.org/2000/svg', ''))
        self.assertIn('cw-wave-arm', character.read_text(encoding='utf-8'))
        self.assertIn('cw-eyes', support.read_text(encoding='utf-8'))

    def test_stylesheet_has_the_animation_dark_mobile_and_reduced_motion_hooks(self):
        css = (BASE / 'static/theme/assets/css/chat-widget.css').read_text(encoding='utf-8')
        for needle in ('.cw-still', 'html.dark .cw-root', '@media (max-width: 1023.98px)', 'safe-area-inset-bottom', '100dvh',
                       '@keyframes cw-wave', '@keyframes cw-float', '@keyframes cw-ring', '--cw-nav'):
            self.assertIn(needle, css)

    def test_script_uses_no_external_urls_and_no_unsafe_html_for_dynamic_text(self):
        js = (BASE / 'static/theme/assets/js/chat-widget.js').read_text(encoding='utf-8')
        self.assertNotIn('http://', js.replace('http://www.w3.org/2000/svg', ''))
        self.assertNotIn('https://', js)
        self.assertNotIn('eval(', js)
        self.assertNotIn('document.write', js)
        # innerHTML فقط برای SVGهای ثابت (آیکون‌های داخلی و آواتار استاتیک) مجاز است
        self.assertEqual(js.count('.innerHTML'), 2)

    def test_script_is_syntactically_valid(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node نصب نیست')
        result = subprocess.run([node, '--check', str(BASE / 'static/theme/assets/js/chat-widget.js')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_chat_is_an_installed_app_since_phase_two(self):
        self.assertIn('chat.apps.ChatConfig', settings.INSTALLED_APPS)
