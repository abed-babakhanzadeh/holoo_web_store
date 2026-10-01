"""
آواتار هدر سایت (templates/base.html) و پنل کاربری: کاربر لاگین‌شده با عکس، همان عکس گرد را می‌بیند؛ بدون عکس، بج دایره‌ای با حرف اول
نام (یا نام خانوادگی، یا شماره‌ی موبایل) - accounts/templatetags/avatar_tags.py::user_initial.
"""
import re

from django.contrib.auth.models import AnonymousUser
from django.template import Context, Template
from django.test import TestCase
from django.urls import reverse

from accounts.models import CustomUser
from accounts.templatetags.avatar_tags import user_badge_glyph, user_initial

PERSON_ICON_PATH = 'M17.982 18.725A7.488 7.488 0 0 0 12 15.75'          # آیکون بی‌نامِ قبلی هدر


def header_button(html):
    start = html.index('id="user-dropdown-button"')
    return html[start:html.index('</button>', start)]


class UserInitialTests(TestCase):
    def initial(self, **fields):
        return user_initial(CustomUser(phone_number=fields.pop('phone_number', '09120000000'), **fields))

    def test_first_name_wins(self):
        self.assertEqual(self.initial(first_name='علی', last_name='رضایی'), 'ع')

    def test_falls_back_to_last_name_when_first_name_is_empty(self):
        self.assertEqual(self.initial(first_name='', last_name='رضایی'), 'ر')
        self.assertEqual(self.initial(first_name=None, last_name='رضایی'), 'ر')
        self.assertEqual(self.initial(first_name='   ', last_name='رضایی'), 'ر')

    def test_phone_number_is_never_used_as_the_letter(self):
        # رقم اول موبایل همیشه «0» است و معنایی ندارد؛ در این حالت بج آیکون آدمک می‌گیرد (user_badge_glyph)
        self.assertEqual(self.initial(first_name=None, last_name=None, phone_number='09123456789'), '')

    def test_leading_spaces_are_ignored_and_latin_letters_are_uppercased(self):
        self.assertEqual(self.initial(first_name='  sara'), 'S')

    def test_never_raises_for_missing_users_or_fields(self):
        self.assertEqual(user_initial(None), '')
        self.assertEqual(user_initial(AnonymousUser()), '')
        self.assertEqual(self.initial(first_name='', last_name='', phone_number=''), '')
        self.assertIn('<svg', str(user_badge_glyph(None)))                                # بدون هیچ داده: آیکون، نه خطا

    def test_template_tag_renders_in_a_template(self):
        user = CustomUser(phone_number='09120000000', first_name='مریم')
        html = Template('{% load avatar_tags %}{% user_initial u %}').render(Context({'u': user}))
        self.assertEqual(html, 'م')


class HeaderAvatarTests(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(phone_number='09140001001', first_name='مریم', last_name='احمدی')

    def page(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_logged_in_user_without_avatar_gets_a_round_badge_with_the_first_letter(self):
        self.client.force_login(self.user)
        button = header_button(self.page())
        self.assertRegex(button, r'<span class="user-badge[^"]*"[^>]*>م</span>')
        self.assertNotIn(PERSON_ICON_PATH, button)                                       # آیکون بی‌نام دیگر نیست
        self.assertNotIn('<img', button)

    def test_badge_uses_last_name_then_phone_when_the_name_is_missing(self):
        self.client.force_login(self.user)
        CustomUser.objects.filter(pk=self.user.pk).update(first_name='')
        self.assertRegex(header_button(self.page()), r'class="user-badge[^"]*"[^>]*>ا</span>')
        CustomUser.objects.filter(pk=self.user.pk).update(last_name=None)
        self.assertRegex(header_button(self.page()), r'class="user-badge[^"]*"[^>]*><svg class="user-badge-icon"')
        self.assertNotRegex(header_button(self.page()), r'class="user-badge[^"]*"[^>]*>0</span>')   # دیگر «0» چاپ نمی‌شود

    def test_user_with_an_avatar_sees_the_round_image_and_no_badge(self):
        CustomUser.objects.filter(pk=self.user.pk).update(avatar='avatars/header-test.png')   # فقط مسیر در دیتابیس؛ فایلی ساخته نمی‌شود
        self.client.force_login(self.user)
        button = header_button(self.page())
        self.assertRegex(button, r'<img src="[^"]*avatars/header-test\.png"[^>]*rounded-full')
        self.assertNotIn('user-badge', button)

    def test_anonymous_visitor_has_no_badge_but_the_login_trigger(self):
        html = self.page()
        self.assertNotIn('user-badge', html)                                             # بج فقط برای کاربر لاگین‌شده است
        self.assertIn('data-modal-target="LoginModal"', html)

    def test_mobile_bottom_nav_shows_a_small_badge_or_the_avatar(self):
        self.client.force_login(self.user)
        html = self.page()
        self.assertTrue(re.search(r'<span class="user-badge is-sm"[^>]*>م</span>', html))
        CustomUser.objects.filter(pk=self.user.pk).update(avatar='avatars/nav-test.png')
        html = self.page()
        self.assertRegex(html, r'<img src="[^"]*avatars/nav-test\.png"[^>]*class="size-6 rounded-full')

    def test_dashboard_sidebar_uses_the_same_fallback_letter(self):
        self.client.force_login(self.user)
        CustomUser.objects.filter(pk=self.user.pk).update(first_name='', last_name='رضایی')
        html = self.client.get(reverse('accounts:dashboard')).content.decode()
        self.assertRegex(html, r'>\s*ر\s*</div>')

    def test_glyph_is_the_initial_for_named_users_and_a_person_icon_for_phone_only_users(self):
        named = CustomUser(phone_number='09120000000', first_name='مریم')
        self.assertEqual(str(user_badge_glyph(named)), 'م')
        phone_only = CustomUser(phone_number='09120000000')
        glyph = str(user_badge_glyph(phone_only))
        self.assertTrue(glyph.startswith('<svg class="user-badge-icon"'))
        self.assertIn('aria-hidden="true"', glyph)

    def test_header_row_is_vertically_centered_not_baseline_aligned(self):
        self.client.force_login(self.user)
        html = self.page()
        block = html[html.index('<!-- login and basket and darkmode -->'):html.index('id="user-dropdown"')]
        self.assertIn('<div class="flex items-center justify-end">', block)
        self.assertIn('<div class="flex items-center md:me-5 me-2">', block)
        self.assertNotIn('items-baseline', block)                                         # علت پایین‌افتادن دکمه‌ی حساب

    def test_long_names_are_clipped_by_a_width_capped_name_span(self):
        CustomUser.objects.filter(pk=self.user.pk).update(first_name='علی', last_name='بلالذدلبیبلابلاب' * 3)
        self.client.force_login(self.user)
        button = header_button(self.page())
        self.assertRegex(button, r'<span class="user-name lg:inline-block hidden"')
        from pathlib import Path
        from django.conf import settings
        css = (Path(settings.BASE_DIR) / 'static/theme/assets/css/app.css').read_text(encoding='utf-8')
        rule = css[css.index('.user-name {'):]
        rule = rule[:rule.index('}')]
        for text in ('max-width: 7.5rem', 'overflow: hidden', 'text-overflow: ellipsis', 'white-space: nowrap'):
            self.assertIn(text, rule)
        self.assertIn('.user-badge-icon { width: 62%; height: 62%; fill: currentColor;', css)

    def test_badge_css_is_defined_for_both_sizes(self):
        from pathlib import Path
        from django.conf import settings
        css = (Path(settings.BASE_DIR) / 'static/theme/assets/css/app.css').read_text(encoding='utf-8')
        for rule in ('.user-badge {', '.user-badge.is-sm {', '.user-badge:not(.is-sm)', 'border-radius: 9999px', 'color: #fff', 'font-weight: 800'):
            self.assertIn(rule, css)
