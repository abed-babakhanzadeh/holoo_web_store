"""
تست‌های مرجع واحد مشخصات فروشگاه (SiteSettings.store_* / map_* / about_*)، صفحه‌های «درباره ما» و
«تماس با ما»، فرم تماس (ثبت پیام + اعلان پیامکی مدیر) و ادمین مربوط.
"""
import tempfile
from unittest import mock

from django.contrib import admin
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from accounts.models import CustomUser
from notifications.models import Notification, NotificationSetting
from notifications.templates_registry import TEMPLATES
from products.contact import MAX_MESSAGES_PER_IP_PER_HOUR, THROTTLE_KEY
from products.models import ContactMessage, SiteSettings, is_valid_iranian_national_code

TINY_GIF = (b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,'
            b'\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;')


def make_national_code(first_nine):
    """ کد ملی معتبر ۱۰ رقمی از ۹ رقم اول (رقم کنترل الگوریتم استاندارد) """
    total = sum(int(first_nine[i]) * (10 - i) for i in range(9))
    remainder = total % 11
    return first_nine + str(remainder if remainder < 2 else 11 - remainder)


class StoreIdentityFieldTests(TestCase):
    def setUp(self):
        SiteSettings.load().save()

    def _settings(self, **changes):
        s = SiteSettings.load()
        for key, value in changes.items():
            setattr(s, key, value)
        return s

    def test_defaults_and_renamed_fields(self):
        s = SiteSettings.load()
        self.assertEqual(s.store_name, 'بازرگانی موسوی')
        self.assertEqual(s.map_type, SiteSettings.MAP_GOOGLE)
        self.assertEqual(s.store_contact_phones, [])
        for old in ('phone', 'email', 'working_hours_text'):
            self.assertFalse(hasattr(s, old), f'{old} باید به store_* تغییر نام داده باشد')
        self.assertIn('شبانه‌روز', s.store_working_hours)

    def test_valid_identity_passes_and_persian_digits_are_normalized(self):
        s = self._settings(
            store_national_id='۱۴۰۰۱۲۳۴۵۶۷', store_registration_number='۱۲۳۴۵', store_economic_code='۴۱۱۱۱۱۱۱۱۱۱۱',
            store_postal_code='۱۲۳۴۵۶۷۸۹۰', store_mobile='۰۹۱۲۳۴۵۶۷۸۹', store_admin_sms_recipient='۰۹۱۹۲۵۱۵۴۶۶',
            store_phone_1='۰۲۵-۳۷۷۰۰۰۰۰',
        )
        s.full_clean()
        self.assertEqual(s.store_national_id, '14001234567')
        self.assertEqual(s.store_postal_code, '1234567890')
        self.assertEqual(s.store_mobile, '09123456789')
        self.assertEqual(s.store_admin_sms_recipient, '09192515466')
        self.assertEqual(s.store_phone_1, '025-37700000')

    def test_national_code_checksum(self):
        good = make_national_code('123456789')
        self.assertTrue(is_valid_iranian_national_code(good))
        self.assertFalse(is_valid_iranian_national_code(good[:-1] + str((int(good[-1]) + 1) % 10)))
        self.assertFalse(is_valid_iranian_national_code('1111111111'))
        s = self._settings(store_national_id=good)
        s.full_clean()
        s = self._settings(store_national_id=good[:-1] + str((int(good[-1]) + 1) % 10))
        with self.assertRaises(ValidationError) as ctx:
            s.full_clean()
        self.assertIn('store_national_id', ctx.exception.message_dict)

    def test_bad_values_are_rejected_per_field(self):
        cases = {
            'store_national_id': '123', 'store_registration_number': 'ab12', 'store_economic_code': '12345',
            'store_postal_code': '12345', 'store_phone_1': 'تلفن!!', 'store_mobile': '0212345678',
            'store_admin_sms_recipient': '9123456789', 'store_admin_sms_recipient_2': '0912', 'store_email_1': 'not-an-email',
        }
        for field, bad in cases.items():
            with self.subTest(field=field):
                s = self._settings(**{field: bad})
                with self.assertRaises(ValidationError) as ctx:
                    s.full_clean()
                self.assertIn(field, ctx.exception.message_dict)

    def test_map_validation(self):
        from decimal import Decimal
        s = self._settings(map_type=SiteSettings.MAP_GOOGLE)               # بدون مختصات: مجاز (فقط نقشه نمایش داده نمی‌شود)
        s.full_clean()
        self.assertEqual(s.map_embed_src, '')
        s = self._settings(map_type=SiteSettings.MAP_GOOGLE, map_latitude=Decimal('95'), map_longitude=Decimal('51'))
        with self.assertRaises(ValidationError) as ctx:
            s.full_clean()
        self.assertIn('map_latitude', ctx.exception.message_dict)
        s = self._settings(map_type=SiteSettings.MAP_NESHAN, map_latitude=Decimal('35.7'), map_longitude=None)
        with self.assertRaises(ValidationError):
            s.full_clean()
        s = self._settings(map_type=SiteSettings.MAP_NESHAN, map_latitude=Decimal('35.689197'), map_longitude=Decimal('51.388974'))
        s.full_clean()
        s = self._settings(map_type=SiteSettings.MAP_CUSTOM, map_iframe_code='')
        with self.assertRaises(ValidationError) as ctx:
            s.full_clean()
        self.assertIn('map_iframe_code', ctx.exception.message_dict)

    def test_map_embed_sources(self):
        from decimal import Decimal
        s = self._settings(map_type=SiteSettings.MAP_GOOGLE, map_latitude=Decimal('35.689197'), map_longitude=Decimal('51.388974'))
        self.assertEqual(s.map_embed_src, 'https://maps.google.com/maps?q=35.689197,51.388974&z=16&output=embed')
        s.map_type = SiteSettings.MAP_NESHAN
        self.assertTrue(s.map_embed_src.startswith('https://www.openstreetmap.org/export/embed.html?bbox='))
        self.assertIn('marker=35.689197,51.388974', s.map_embed_src)
        self.assertEqual(s.map_directions_url, 'https://www.google.com/maps/dir/?api=1&destination=35.689197,51.388974')
        s.map_latitude = None
        self.assertEqual(s.map_embed_src, '')
        self.assertEqual(s.map_directions_url, '')

    def test_custom_iframe_only_allows_https_on_allowed_hosts(self):
        good = '<iframe src="https://www.google.com/maps/embed?pb=!1m18" width="600"></iframe>'
        s = self._settings(map_type=SiteSettings.MAP_CUSTOM, map_iframe_code=good)
        self.assertEqual(s.map_embed_src, 'https://www.google.com/maps/embed?pb=!1m18')
        s.full_clean()
        evil = [
            '<iframe src="http://www.google.com/maps/embed"></iframe>',               # بدون https
            '<iframe src="https://evil.example/maps"></iframe>',                      # دامنه‌ی غیرمجاز
            '<iframe src="https://www.google.com/search?q=x"></iframe>',              # گوگل ولی نه /maps
            '<iframe src="javascript:alert(1)"></iframe>',
            '<script>alert(1)</script>',
            '<img src=x onerror=alert(1)>',
        ]
        for code in evil:
            with self.subTest(code=code):
                s = self._settings(map_type=SiteSettings.MAP_CUSTOM, map_iframe_code=code)
                self.assertEqual(s.map_embed_src, '')
                with self.assertRaises(ValidationError):
                    s.full_clean()
        # فقط src استخراج می‌شود؛ بقیه‌ی کد (مثلاً onload) هرگز رندر نمی‌شود
        s = self._settings(map_type=SiteSettings.MAP_CUSTOM,
                           map_iframe_code='<iframe onload="steal()" src="https://www.google.com/maps/embed?a=1"></iframe>')
        self.assertEqual(s.map_embed_src, 'https://www.google.com/maps/embed?a=1')

    def test_working_hours_rows_and_phone_links(self):
        s = self._settings(
            store_working_hours='شنبه تا چهارشنبه: ۸ تا ۱۷\n\nجمعه: تعطیل\nپاسخگوی تلفنی نیستیم',
            store_phone_1='025-37700000', store_phone_2='', store_mobile='09123456789',
        )
        self.assertEqual(s.store_working_hours_rows, [
            ('شنبه تا چهارشنبه', '۸ تا ۱۷'), ('جمعه', 'تعطیل'), ('', 'پاسخگوی تلفنی نیستیم'),
        ])
        self.assertEqual(s.store_contact_phones, [
            {'display': '025-37700000', 'href': 'tel:02537700000'},
            {'display': '09123456789', 'href': 'tel:09123456789'},
        ])

    def test_about_content_helpers_hide_incomplete_items(self):
        s = self._settings(
            about_story_text='پاراگراف اول\nادامه‌ی اول\n\nپاراگراف دوم',
            about_value1_title='کیفیت', about_value1_text='متن ۱', about_value2_title='', about_value2_text='',
            about_value3_title='', about_value3_text='فقط متن',
            about_stat1_value='۱۰+', about_stat1_label='سال سابقه', about_stat2_value='۵۰۰', about_stat2_label='',
        )
        self.assertEqual(s.about_story_paragraphs, ['پاراگراف اول\nادامه‌ی اول', 'پاراگراف دوم'])
        self.assertEqual([c['title'] for c in s.about_value_cards], ['کیفیت', ''])
        self.assertTrue(all(c['icon_path'] for c in s.about_value_cards))
        self.assertEqual(len(s.about_stats), 1)
        self.assertEqual(
            s.about_stats[0],
            {'value': '۱۰+', 'label': 'سال سابقه', 'target': 10, 'digits': '۱۰', 'display': '۱۰', 'prefix': '',
             'suffix': '+', 'fa_digits': True},
        )

    def test_about_stats_counter_metadata(self):
        s = self._settings(
            about_stat1_value='۹۸٪', about_stat1_label='رضایت', about_stat2_value='5000+', about_stat2_label='مشتری',
            about_stat3_value='همیشه', about_stat3_label='در دسترس', about_stat4_value='سال ۱۳۹۰', about_stat4_label='تأسیس',
        )
        stats = s.about_stats
        self.assertEqual((stats[0]['target'], stats[0]['suffix'], stats[0]['fa_digits']), (98, '٪', True))
        self.assertEqual((stats[1]['target'], stats[1]['suffix'], stats[1]['fa_digits']), (5000, '+', False))
        # جداکننده‌ی هزارگان: فارسی «٬» با ارقام فارسی، لاتین «,»
        self.assertEqual((stats[0]['display'], stats[1]['display']), ('۹۸', '5,000'))
        self.assertIsNone(stats[2]['target'])                                # متن غیرعددی: بدون شمارنده، همان متن
        self.assertEqual((stats[3]['prefix'], stats[3]['target']), ('سال ', 1390))

    def test_grouped_number_formatting_and_separators_in_admin_input(self):
        from products.models import format_grouped_number
        self.assertEqual(format_grouped_number(5000, True), '۵٬۰۰۰')
        self.assertEqual(format_grouped_number(1200000, True), '۱٬۲۰۰٬۰۰۰')
        self.assertEqual(format_grouped_number(5000, False), '5,000')
        self.assertEqual(format_grouped_number(999, True), '۹۹۹')
        s = self._settings(
            about_stat1_value='۵٬۰۰۰+', about_stat1_label='الف', about_stat2_value='5,000+', about_stat2_label='ب',
            about_stat3_value='۵۰۰۰+', about_stat3_label='ج',
        )
        self.assertEqual([(x['target'], x['display'], x['suffix']) for x in s.about_stats],
                         [(5000, '۵٬۰۰۰', '+'), (5000, '5,000', '+'), (5000, '۵٬۰۰۰', '+')])

    def test_two_admin_recipients_are_deduplicated_and_ordered(self):
        s = self._settings(store_admin_sms_recipient='09191112233', store_admin_sms_recipient_2='09194445566')
        self.assertEqual(s.store_admin_sms_recipients, ['09191112233', '09194445566'])
        s = self._settings(store_admin_sms_recipient='09191112233', store_admin_sms_recipient_2='09191112233')
        self.assertEqual(s.store_admin_sms_recipients, ['09191112233'])
        s = self._settings(store_admin_sms_recipient='', store_admin_sms_recipient_2='09194445566')
        self.assertEqual(s.store_admin_sms_recipients, ['09194445566'])
        s = self._settings(store_admin_sms_recipient='', store_admin_sms_recipient_2='')
        self.assertEqual(s.store_admin_sms_recipients, [])
        s = self._settings(store_admin_sms_recipient_2='۰۹۱۹۴۴۴۵۵۶۶')
        s.full_clean()
        self.assertEqual(s.store_admin_sms_recipient_2, '09194445566')       # ارقام فارسی نرمال می‌شود
        s = self._settings(store_admin_sms_recipient_2='123')
        with self.assertRaises(ValidationError) as ctx:
            s.full_clean()
        self.assertIn('store_admin_sms_recipient_2', ctx.exception.message_dict)

    def test_db_constraint_blocks_invalid_map_type(self):
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError), transaction.atomic():
            SiteSettings.objects.filter(pk=1).update(map_type='bogus')


class StoreSettingsAdminTests(TestCase):
    def setUp(self):
        SiteSettings.load().save()
        self.admin_user = CustomUser.objects.create_superuser(phone_number='09120005040')
        self.client.force_login(self.admin_user)

    def test_settings_page_has_new_groups_and_fields(self):
        response = self.client.get(reverse('admin:products_sitesettings_change', args=[1]))
        self.assertEqual(response.status_code, 200)
        keys = [g['key'] for g in response.context['sitesettings_groups']]
        self.assertIn('store', keys)
        self.assertIn('about', keys)
        for name in ('store_name', 'store_legal_name', 'store_national_id', 'store_registration_number',
                     'store_economic_code', 'store_postal_code', 'store_address', 'store_phone_1', 'store_phone_2',
                     'store_mobile', 'store_admin_sms_recipient', 'store_email_1', 'store_email_2',
                     'store_working_hours', 'map_type', 'map_latitude', 'map_longitude', 'map_iframe_code',
                     'about_story_title', 'about_story_text', 'about_story_image', 'about_value1_title',
                     'about_value3_icon', 'about_stat4_label', 'instagram_url', 'telegram_url', 'samandehi_link'):
            with self.subTest(field=name):
                self.assertContains(response, f'name="{name}"')

    def test_contact_message_admin_is_read_only_for_sender_data_and_stamps_reply(self):
        message = ContactMessage.objects.create(name='علی', phone='09123456789', subject='سوال', message='متن')
        model_admin = admin.site._registry[ContactMessage]
        self.assertFalse(model_admin.has_add_permission(mock.Mock()))
        url = reverse('admin:products_contactmessage_change', args=[message.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="message"')                 # متن پیام فقط‌خواندنی
        response = self.client.post(url, {'status': ContactMessage.STATUS_NEW, 'admin_reply': 'پاسخ ما'})
        self.assertEqual(response.status_code, 302)
        message.refresh_from_db()
        self.assertEqual(message.admin_reply, 'پاسخ ما')
        self.assertIsNotNone(message.replied_at)
        self.assertEqual(message.status, ContactMessage.STATUS_ANSWERED)

    def test_contact_message_changelist_and_actions(self):
        message = ContactMessage.objects.create(name='علی', email='a@example.com', subject='سوال', message='متن')
        response = self.client.get(reverse('admin:products_contactmessage_changelist'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'a@example.com')
        self.client.post(reverse('admin:products_contactmessage_changelist'), {
            'action': 'mark_closed', '_selected_action': [message.pk],
        })
        message.refresh_from_db()
        self.assertEqual(message.status, ContactMessage.STATUS_CLOSED)


class AboutAndContactPageTests(TestCase):
    def setUp(self):
        self._media = tempfile.TemporaryDirectory()
        self.addCleanup(self._media.cleanup)
        override = override_settings(MEDIA_ROOT=self._media.name)
        override.enable()
        self.addCleanup(override.disable)
        SiteSettings.load().save()

    def _fill(self, **extra):
        from decimal import Decimal
        values = dict(
            store_name='فروشگاه آزمون', store_address='قم، خیابان آزمون، پلاک ۱', store_postal_code='1234567890',
            store_phone_1='025-37700000', store_phone_2='025-37700001', store_mobile='09123456789',
            store_email_1='info@example.com', store_email_2='support@example.com',
            store_working_hours='شنبه تا چهارشنبه: ۸ تا ۱۷\nجمعه: تعطیل',
            map_type=SiteSettings.MAP_GOOGLE, map_latitude=Decimal('34.6399'), map_longitude=Decimal('50.8759'),
        )
        values.update(extra)
        s = SiteSettings.load()
        for k, v in values.items():
            setattr(s, k, v)
        s.save()
        return s

    def test_contact_page_renders_live_settings(self):
        self._fill()
        response = self.client.get(reverse('products:contact_us'))
        self.assertEqual(response.status_code, 200)
        for text in ('قم، خیابان آزمون، پلاک ۱', '025-37700000', '025-37700001', '09123456789', 'info@example.com',
                     'support@example.com', 'شنبه تا چهارشنبه', '۸ تا ۱۷', 'جمعه', 'تعطیل', '1234567890',
                     'تماس با فروشگاه آزمون', 'https://maps.google.com/maps?q=34.639900,50.875900&amp;z=16&amp;output=embed'):
            with self.subTest(text=text):
                self.assertContains(response, text)
        self.assertContains(response, 'csrfmiddlewaretoken')
        self.assertContains(response, 'href="tel:02537700000"')

    def test_contact_page_without_data_has_no_map_and_shows_hint(self):
        s = SiteSettings.load()
        s.store_working_hours = ''
        s.save()
        response = self.client.get(reverse('products:contact_us'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '<iframe')
        self.assertContains(response, 'اطلاعات تماس هنوز ثبت نشده است')
        self.assertNotContains(response, 'ساعات کاری')

    def test_map_choices_render_the_right_iframe_and_buttons(self):
        self._fill(map_type=SiteSettings.MAP_NESHAN, map_neshan_url='https://balad.ir/p/abc')
        response = self.client.get(reverse('products:contact_us'))
        self.assertContains(response, 'https://www.openstreetmap.org/export/embed.html')
        self.assertContains(response, 'href="https://balad.ir/p/abc"')
        self.assertContains(response, 'مسیریابی در گوگل‌مپ')
        self._fill(map_type=SiteSettings.MAP_CUSTOM,
                   map_iframe_code='<iframe src="https://www.google.com/maps/embed?pb=1" onload="x()"></iframe>')
        response = self.client.get(reverse('products:contact_us'))
        self.assertContains(response, 'src="https://www.google.com/maps/embed?pb=1"')
        self.assertNotContains(response, 'onload')

    def test_about_page_shows_content_and_hides_empty_sections(self):
        s = SiteSettings.load()
        s.about_story_text = 'داستان ما از سال ۱۳۹۰ شروع شد.\n\nامروز هم ادامه دارد.'
        s.about_value1_title = 'کیفیت عالی'
        s.about_value1_text = 'متن کارت اول'
        s.about_stat1_value, s.about_stat1_label = '۱۰+', 'سال سابقه'
        s.about_story_image = SimpleUploadedFile('story.gif', TINY_GIF, content_type='image/gif')
        s.store_legal_name = 'شرکت آزمون'
        s.store_national_id = '14001234567'
        s.save()
        response = self.client.get(reverse('products:about_us'))
        self.assertEqual(response.status_code, 200)
        for text in ('داستان ما از سال ۱۳۹۰ شروع شد.', 'امروز هم ادامه دارد.', 'کیفیت عالی', 'متن کارت اول', '>۱۰</span>',
                     'سال سابقه', 'شرکت آزمون', '14001234567', 'مشخصات ثبتی', 'ماموریت و ارزش‌های ما'):
            with self.subTest(text=text):
                self.assertContains(response, text)
        self.assertContains(response, '/media/about/story')
        self.assertNotContains(response, 'تیم متخصص')                     # بخش تیمِ ساختگی نسخه‌ی خام نیامده
        # فقط یک کارت و یک آمار پر شده؛ بقیه رندر نمی‌شوند
        html = response.content.decode()
        self.assertEqual(html.count('<article class="about-card'), 1)
        self.assertEqual(html.count('class="about-stat reveal'), 1)
        # شمارنده: عدد هدف + ارقام فارسی؛ متن اولیه (بدون JS) همان عدد است
        self.assertIn('data-target="10" data-fa="1">۱۰</span>', html)
        self.assertIn('\\u066C', html)                                        # جداکننده‌ی هزارگان فارسی در شمارنده
        self.assertIn('<span>+</span>', html)
        self.assertIn('reveal from-start', html)                             # ورود از راست/چپ هنگام اسکرول
        self.assertIn('reveal from-end', html)
        self.assertIn('IntersectionObserver', html)

    def test_about_page_empty_state(self):
        s = SiteSettings.load()
        s.about_story_text = ''
        s.save()
        response = self.client.get(reverse('products:about_us'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'محتوای این صفحه به‌زودی تکمیل می‌شود.')
        self.assertNotContains(response, 'ماموریت و ارزش‌های ما')

    def test_store_name_and_contact_flow_into_header_footer_and_titles(self):
        self._fill()
        s = SiteSettings.load()
        s.instagram_url, s.telegram_url, s.samandehi_link = (
            'https://instagram.com/test', 'https://t.me/test', 'https://samandehi.example/x')
        s.save()
        html = self.client.get(reverse('products:home')).content.decode()
        self.assertIn('فروشگاه آزمون - صفحه اصلی', html)
        self.assertIn('<title>فروشگاه آزمون</title>', html)
        self.assertNotIn('بازرگانی موسوی', html)
        for text in ('025-37700000', '09123456789', 'info@example.com', 'support@example.com',
                     'قم، خیابان آزمون، پلاک ۱', 'شنبه تا چهارشنبه'):
            self.assertIn(text, html)
        self.assertIn(reverse('products:about_us'), html)
        self.assertIn(reverse('products:contact_us'), html)
        self.assertIn('social/instagram.svg', html)
        self.assertIn('social/telegram.svg', html)
        self.assertIn('نماد ساماندهی', html)

    def test_main_menu_has_categories_shop_blog_about_contact_in_order(self):
        from products.models import Category
        Category.objects.create(name='دسته‌ی منو', slug='menu-cat')           # مگامنو فقط وقتی دسته هست رندر می‌شود
        html = self.client.get(reverse('products:home')).content.decode()
        nav = html[html.index('id="megaMenu"'):html.index('<!-- ================= end header')]
        labels = ['دسته‌بندی‌ها', 'فروشگاه', 'وبلاگ', 'درباره ما', 'تماس با ما']
        positions = [nav.index(label) for label in labels]
        self.assertEqual(positions, sorted(positions))
        # «دسته‌بندی‌ها» لینک نیست (فقط هاور مگامنو)؛ «فروشگاه» به صفحه‌ی فروشگاه می‌رود
        trigger = nav[nav.index('id="mega-menu-fire"'):nav.index('دسته‌بندی‌ها')]
        self.assertNotIn(f'href="{reverse("products:product_list")}"', trigger)
        self.assertIn('role="button"', trigger)
        import re
        self.assertTrue(re.search(r'<a href="%s"[^>]*>.*?فروشگاه\s*</a>' % re.escape(reverse('products:product_list')), nav, re.S))
        for url in (reverse('blog:list'), reverse('products:about_us'), reverse('products:contact_us')):
            self.assertIn(f'href="{url}"', nav)
        self.assertIn('id="mega-menu-fire-target"', nav)                    # مگامنو همچنان دست‌نخورده

    def test_mobile_drawer_lists_about_and_contact(self):
        html = self.client.get(reverse('products:home')).content.decode()
        drawer = html[html.index('id="offcanvas-right"'):]
        drawer = drawer[:drawer.index('</nav>')]
        self.assertIn(reverse('products:about_us'), drawer)
        self.assertIn(reverse('products:contact_us'), drawer)

    def test_pages_are_in_the_sitemap(self):
        response = self.client.get('/sitemap.xml')
        content = response.content.decode()
        self.assertIn(reverse('products:about_us'), content)
        self.assertIn(reverse('products:contact_us'), content)


class ContactFormTests(TestCase):
    URL_NAME = 'products:contact_us'

    def setUp(self):
        SiteSettings.load().save()
        self._clear_throttle()
        self.addCleanup(self._clear_throttle)

    def _clear_throttle(self):
        cache.delete(THROTTLE_KEY.format(ip='127.0.0.1'))

    def _data(self, **extra):
        data = {'name': 'علی رضایی', 'phone': '09123456789', 'email': '', 'subject': 'سوال درباره‌ی ارسال',
                'message': 'سلام، زمان ارسال سفارش چقدر است؟', 'website': ''}
        data.update(extra)
        return data

    def _post(self, **extra):
        return self.client.post(reverse(self.URL_NAME), self._data(**extra))

    def test_valid_submission_is_saved_with_ip_and_redirects_with_success_message(self):
        response = self._post()
        # fetch_redirect_response=False: وگرنه assertRedirects خودش صفحه‌ی مقصد را می‌گیرد و پیام فلش را مصرف می‌کند
        self.assertRedirects(response, reverse(self.URL_NAME), fetch_redirect_response=False)
        message = ContactMessage.objects.get()
        self.assertEqual((message.name, message.phone, message.subject, message.status),
                         ('علی رضایی', '09123456789', 'سوال درباره‌ی ارسال', ContactMessage.STATUS_NEW))
        self.assertEqual(message.ip_address, '127.0.0.1')
        self.assertIsNone(message.user)
        page = self.client.get(reverse(self.URL_NAME))
        self.assertContains(page, 'پیام شما با موفقیت ثبت شد')
        again = self.client.get(reverse(self.URL_NAME))                      # پیام فقط یک بار نمایش داده می‌شود
        self.assertNotContains(again, 'پیام شما با موفقیت ثبت شد')

    def test_persian_digit_mobile_is_normalized_and_user_is_linked(self):
        user = CustomUser.objects.create_user(phone_number='09120005050')
        self.client.force_login(user)
        self._post(phone='۰۹۱۲-۳۴۵-۶۷۸۹')
        message = ContactMessage.objects.get()
        self.assertEqual(message.phone, '09123456789')
        self.assertEqual(message.user, user)

    def test_email_only_is_enough_but_neither_is_rejected(self):
        self._post(phone='', email='a@example.com')
        self.assertEqual(ContactMessage.objects.count(), 1)
        response = self._post(phone='', email='')
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'حداقل یکی از', status_code=400)
        self.assertEqual(ContactMessage.objects.count(), 1)

    def test_invalid_fields_are_rejected_with_messages_and_values_are_kept(self):
        for field, bad, expected in (
            ('phone', '123', 'شماره موبایل واردشده معتبر نیست'),
            ('email', 'nope', 'ایمیل واردشده معتبر نیست'),
            ('name', '', 'نام خود را وارد کنید'),
            ('subject', '   ', 'موضوع پیام را وارد کنید'),
            ('message', '', 'متن پیام را وارد کنید'),
            ('message', 'x' * 2001, '2000'),
        ):
            with self.subTest(field=field, bad=bad[:5]):
                response = self._post(**{field: bad})
                self.assertEqual(response.status_code, 400)
                self.assertContains(response, expected, status_code=400)
        self.assertEqual(ContactMessage.objects.count(), 0)
        response = self._post(email='nope')
        self.assertContains(response, 'value="علی رضایی"', status_code=400)       # ورودی‌های درست حفظ می‌شود

    def test_honeypot_pretends_success_but_saves_nothing(self):
        response = self._post(website='http://spam.example')
        self.assertRedirects(response, reverse(self.URL_NAME))
        self.assertEqual(ContactMessage.objects.count(), 0)
        self.assertEqual(Notification.objects.filter(template_key='contact_message_admin').count(), 0)

    def test_throttle_blocks_after_the_hourly_limit(self):
        for _ in range(MAX_MESSAGES_PER_IP_PER_HOUR):
            self.assertEqual(self._post().status_code, 302)
        response = self._post()
        self.assertEqual(response.status_code, 429)
        self.assertContains(response, 'بیش از حد مجاز', status_code=429)
        self.assertEqual(ContactMessage.objects.count(), MAX_MESSAGES_PER_IP_PER_HOUR)

    def test_csrf_token_is_enforced(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(reverse(self.URL_NAME), self._data())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(ContactMessage.objects.count(), 0)

    def test_sms_goes_to_both_admin_recipients_once_each(self):
        s = SiteSettings.load()
        s.store_admin_sms_recipient, s.store_admin_sms_recipient_2 = '09191112233', '09194445566'
        s.save()
        self._post()
        recipients = sorted(Notification.objects.filter(template_key='contact_message_admin').values_list('recipient', flat=True))
        self.assertEqual(recipients, ['09191112233', '09194445566'])

    def test_same_number_in_both_fields_gets_a_single_sms(self):
        s = SiteSettings.load()
        s.store_admin_sms_recipient = s.store_admin_sms_recipient_2 = '09191112233'
        s.save()
        self._post()
        self.assertEqual(Notification.objects.filter(template_key='contact_message_admin').count(), 1)

    def test_failure_for_one_recipient_does_not_block_the_other(self):
        s = SiteSettings.load()
        s.store_admin_sms_recipient, s.store_admin_sms_recipient_2 = '09191112233', '09194445566'
        s.save()
        from notifications.service import notify as real_notify

        def flaky(to, *args, **kwargs):
            if to == '09191112233':
                raise RuntimeError('boom')
            return real_notify(to, *args, **kwargs)

        with mock.patch('notifications.receivers.notify', side_effect=flaky):
            response = self._post()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ContactMessage.objects.count(), 1)
        self.assertEqual(
            list(Notification.objects.filter(template_key='contact_message_admin').values_list('recipient', flat=True)),
            ['09194445566'],
        )

    def test_sms_goes_to_the_store_admin_recipient_with_name_and_subject(self):
        s = SiteSettings.load()
        s.store_admin_sms_recipient = '09191112233'
        s.save()
        self._post()
        notification = Notification.objects.get(template_key='contact_message_admin')
        self.assertEqual(notification.recipient, '09191112233')
        self.assertIn('علی رضایی', notification.text)
        self.assertIn('سوال درباره‌ی ارسال', notification.text)

    @override_settings(ADMIN_NOTIFICATION_RECIPIENT='09190000000')
    def test_sms_falls_back_to_the_server_default_recipient(self):
        self._post()
        self.assertEqual(Notification.objects.get(template_key='contact_message_admin').recipient, '09190000000')

    def test_sms_text_is_sanitized_and_limited(self):
        s = SiteSettings.load()
        s.store_admin_sms_recipient = '09191112233'
        s.save()
        self._post(name='حمید\nhttps://phish.example/x', subject='الف' * 40)
        text = Notification.objects.get(template_key='contact_message_admin').text
        self.assertNotIn('phish.example', text)
        self.assertNotIn('\n', text.split('«', 1)[1].rsplit('»', 1)[0])
        self.assertIn('[لینک]', text)
        self.assertIn('…', text)
        self.assertLess(len(text), 220)

    # ---- مشتری واردشده: تأییدشده از پروفایل می‌خواند، تأییدنشده پیش‌فرض پر + قابل ویرایش ----
    def _approved(self, **extra):
        from accounts.testing import make_approved_user
        extra.setdefault('first_name', 'مریم')
        extra.setdefault('last_name', 'کاظمی')
        extra.setdefault('email', 'maryam@example.com')
        return make_approved_user('09125550001', **extra)

    def test_approved_customer_does_not_see_identity_fields(self):
        self.client.force_login(self._approved())
        response = self.client.get(reverse(self.URL_NAME))
        self.assertEqual(response.status_code, 200)
        for name in ('name', 'phone', 'email'):
            self.assertNotContains(response, f'name="{name}"')
        for name in ('subject', 'message', 'website'):
            self.assertContains(response, f'name="{name}"')
        self.assertContains(response, 'پیام شما با مشخصات حساب کاربری‌تان ارسال می‌شود')
        self.assertContains(response, 'مریم کاظمی')
        self.assertContains(response, '09125550001')

    def test_approved_customer_message_uses_profile_and_ignores_posted_identity(self):
        user = self._approved()
        self.client.force_login(user)
        response = self.client.post(reverse(self.URL_NAME), {
            'subject': 'سوال', 'message': 'متن پیام', 'website': '',
            'name': 'جعلی', 'phone': '09999999999', 'email': 'fake@example.com',      # باید نادیده گرفته شوند
        })
        self.assertRedirects(response, reverse(self.URL_NAME), fetch_redirect_response=False)
        message = ContactMessage.objects.get()
        self.assertEqual((message.name, message.phone, message.email), ('مریم کاظمی', '09125550001', 'maryam@example.com'))
        self.assertEqual(message.user, user)

    def test_approved_customer_without_email_sends_with_phone_only(self):
        # تأیید یعنی پروفایل کامل (نام و کد ملی)؛ ایمیل اختیاری است و خالی ذخیره می‌شود
        user = self._approved(email=None)
        self.client.force_login(user)
        self.client.post(reverse(self.URL_NAME), {'subject': 'سوال', 'message': 'متن', 'website': ''})
        message = ContactMessage.objects.get()
        self.assertEqual((message.name, message.phone, message.email), ('مریم کاظمی', '09125550001', ''))

    def test_approved_customer_sms_carries_profile_name(self):
        s = SiteSettings.load()
        s.store_admin_sms_recipient = '09191112233'
        s.save()
        self.client.force_login(self._approved())
        self.client.post(reverse(self.URL_NAME), {'subject': 'ارسال سفارش', 'message': 'متن', 'website': ''})
        text = Notification.objects.get(template_key='contact_message_admin').text
        self.assertIn('مریم کاظمی', text)
        self.assertIn('ارسال سفارش', text)

    def test_approved_customer_still_needs_subject_and_message(self):
        self.client.force_login(self._approved())
        response = self.client.post(reverse(self.URL_NAME), {'subject': '', 'message': '', 'website': ''})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'موضوع پیام را وارد کنید', status_code=400)
        self.assertContains(response, 'پیام شما با مشخصات حساب کاربری‌تان ارسال می‌شود', status_code=400)
        self.assertEqual(ContactMessage.objects.count(), 0)

    def test_unapproved_customer_sees_prefilled_editable_fields(self):
        user = CustomUser.objects.create_user(
            phone_number='09125550002', first_name='رضا', last_name='احمدی', email='reza@example.com')
        self.client.force_login(user)
        response = self.client.get(reverse(self.URL_NAME))
        for name in ('name', 'phone', 'email'):
            self.assertContains(response, f'name="{name}"')
        self.assertContains(response, 'value="رضا احمدی"')
        self.assertContains(response, 'value="09125550002"')
        self.assertContains(response, 'value="reza@example.com"')
        self.assertNotContains(response, 'پیام شما با مشخصات حساب کاربری‌تان ارسال می‌شود')

    def test_unapproved_customer_without_profile_gets_phone_prefilled_only(self):
        self.client.force_login(CustomUser.objects.create_user(phone_number='09125550003'))
        response = self.client.get(reverse(self.URL_NAME))
        self.assertContains(response, 'value="09125550003"')
        self.assertNotContains(response, 'value="None"')

    def test_unapproved_customer_edited_values_are_used(self):
        user = CustomUser.objects.create_user(phone_number='09125550004', first_name='رضا', last_name='احمدی')
        self.client.force_login(user)
        self._post(name='نام ویرایش‌شده', phone='09126660000')
        message = ContactMessage.objects.get()
        self.assertEqual((message.name, message.phone, message.user), ('نام ویرایش‌شده', '09126660000', user))

    def test_anonymous_visitor_still_sees_empty_fields(self):
        response = self.client.get(reverse(self.URL_NAME))
        for name in ('name', 'phone', 'email'):
            self.assertContains(response, f'name="{name}"')
        self.assertNotContains(response, 'پیام شما با مشخصات حساب کاربری‌تان ارسال می‌شود')
        self.assertNotContains(response, 'value="09')

    def test_notification_failure_does_not_break_the_submission(self):
        with mock.patch('notifications.receivers.notify', side_effect=RuntimeError('sms down')), \
                mock.patch('notifications.receivers.notify_admin', side_effect=RuntimeError('sms down')):
            response = self._post()
        self.assertRedirects(response, reverse(self.URL_NAME))
        self.assertEqual(ContactMessage.objects.count(), 1)

    def test_template_key_is_registered_and_seeded(self):
        self.assertIn('contact_message_admin', TEMPLATES)
        self.assertTrue(NotificationSetting.objects.filter(template_key='contact_message_admin').exists())

    def test_disabled_notification_setting_still_saves_the_message(self):
        NotificationSetting.objects.filter(template_key='contact_message_admin').update(is_enabled=False)
        self._post()
        self.assertEqual(ContactMessage.objects.count(), 1)
        self.assertEqual(Notification.objects.filter(template_key='contact_message_admin').count(), 0)
