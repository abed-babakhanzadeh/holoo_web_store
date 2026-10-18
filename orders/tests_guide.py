"""
صفحه‌های «راهنمای خرید» (نحوه ثبت سفارش، رویه ارسال، شیوه‌های پرداخت، رویه بازگرداندن کالا)، لینک‌های فوتر و متن «چرا ...؟» صفحه اصلی.
"""
from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from locations.models import City, DeliveryZone, Province
from products.models import Category, Product, SiteSettings
from promotions.models import DiscountPolicy, FreeShippingRule
from promotions.testing import reset_promotions_cache
from returns.models import ReturnabilityRule, ReturnReason

PAGES = ('guide:order', 'guide:shipping', 'guide:payment', 'returns:procedure')


class GuideBase(TestCase):
    def setUp(self):
        self.addCleanup(cache.delete, SiteSettings.CACHE_KEY)

    def configure(self, **fields):
        SiteSettings.objects.update_or_create(pk=1, defaults=fields)
        cache.delete(SiteSettings.CACHE_KEY)

    def get(self, name):
        response = self.client.get(reverse(name))
        self.assertEqual(response.status_code, 200)
        return response


class PagesTests(GuideBase):
    def test_every_page_renders_for_a_guest_with_the_tab_bar_and_a_real_title(self):
        for name in PAGES:
            with self.subTest(page=name):
                html = self.get(name).content.decode()
                for target in PAGES:
                    self.assertIn(f'href="{reverse(target)}"', html)
                self.assertEqual(html.count('aria-current="page"'), 1)
                self.assertIn('class="guide-hero"', html)
                self.assertNotIn('لورم', html)

    def test_current_tab_is_marked_per_page(self):
        html = self.get('guide:shipping').content.decode()
        self.assertRegex(html, rf'href="{reverse("guide:shipping")}" aria-current="page"')

    def test_footer_links_are_real_pages_not_placeholders(self):
        html = self.get('products:home').content.decode()
        footer = html[html.index('راهنمای خرید'):]
        for label, name in (('نحوه ثبت سفارش', 'guide:order'), ('رویه ارسال سفارش', 'guide:shipping'),
                            ('شیوه‌های پرداخت', 'guide:payment'), ('رویه‌های بازگرداندن کالا', 'returns:procedure')):
            self.assertIn(f'<a href="{reverse(name)}">{label}</a>', footer)
        for label in ('نحوه ثبت سفارش', 'رویه ارسال سفارش', 'شیوه‌های پرداخت'):
            self.assertNotIn(f'<a href="#">{label}</a>', footer)

    def test_footer_carries_the_developer_credit_with_a_safe_external_link(self):
        html = self.get('products:home').content.decode()
        credit = html[html.index('data-testid="footer-credit"'):]
        credit = credit[:credit.index('</p>')]
        self.assertIn('طراحی و توسعه', credit)
        self.assertIn('href="https://mindos.ir/"', credit)
        self.assertIn('target="_blank"', credit)
        self.assertIn('rel="noopener"', credit)
        self.assertIn('>MINDOS<', credit)

    def test_pages_are_in_the_sitemap(self):
        body = self.client.get('/sitemap.xml').content.decode()
        for name in PAGES:
            self.assertIn(reverse(name), body)

    def test_contact_card_uses_the_store_profile(self):
        self.configure(store_phone_1='02537718634', store_email_1='shop@example.com', store_address='قم - خیابان نمونه')
        html = self.get('guide:order').content.decode()
        for value in ('02537718634', 'shop@example.com', 'قم - خیابان نمونه'):
            self.assertIn(value, html)


class HowToOrderTests(GuideBase):
    def test_reservation_window_and_return_period_come_from_the_system(self):
        self.configure(return_period_days=10, return_period_unit='calendar_days', cheque_submission_deadline_hours=48)
        html = self.get('guide:order').content.decode()
        self.assertIn('20 دقیقه', html)
        self.assertIn('10 روز تقویمی', html)
        self.assertIn('48 ساعت', html)

    def test_cheque_deadline_sentence_is_dropped_when_the_deadline_is_off(self):
        self.configure(cheque_submission_deadline_hours=0)
        html = self.get('guide:order').content.decode()
        self.assertNotIn('0 ساعت', html)

    def test_login_link_only_for_guests(self):
        self.assertIn('ورود / ثبت‌نام', self.get('guide:order').content.decode())


class ShippingTests(GuideBase):
    def setUp(self):
        super().setUp()
        reset_promotions_cache()
        self.addCleanup(reset_promotions_cache)
        province = Province.objects.create(name='استان راهنما')
        self.city = City.objects.create(province=province, name='شهر راهنما')

    def test_courier_zones_with_tariffs_are_listed_and_unset_or_inactive_ones_are_not(self):
        DeliveryZone.objects.create(city=self.city, name='ناحیه الف', shipping_cost=50000)
        DeliveryZone.objects.create(city=self.city, name='ناحیه بی‌تعرفه', shipping_cost=0)
        DeliveryZone.objects.create(city=self.city, name='ناحیه غیرفعال', shipping_cost=70000, is_active=False)
        html = self.get('guide:shipping').content.decode()
        self.assertIn('ناحیه الف', html)
        self.assertIn('50,000', html)
        self.assertNotIn('ناحیه بی‌تعرفه', html)
        self.assertNotIn('ناحیه غیرفعال', html)
        self.assertIn('برای آدرس‌های شهر راهنما', html)

    def test_no_zones_means_no_tariff_table(self):
        html = self.get('guide:shipping').content.decode()
        self.assertNotIn('هزینه‌ی ارسال با پیک</h2>', html)

    def test_postage_section_follows_the_site_switch_and_label(self):
        self.configure(postage_collect_enabled=True, postage_collect_label='پس‌کرایه تستی')
        self.assertIn('پس‌کرایه تستی', self.get('guide:shipping').content.decode())
        self.configure(postage_collect_enabled=False)
        html = self.get('guide:shipping').content.decode()
        self.assertIn('ارسال با پست در حال حاضر غیرفعال است', html)
        self.assertNotIn('پس‌کرایه تستی', html)

    def test_active_free_shipping_rules_are_listed_with_scope(self):
        DiscountPolicy.load()
        rule = FreeShippingRule.objects.create(title='ارسال رایگان نوروز', is_active=True, min_cart_total=2000000,
                                               postage_mode=FreeShippingRule.POSTAGE_COVER)
        rule.cities.add(self.city)
        FreeShippingRule.objects.create(title='قاعده‌ی منقضی', is_active=True, ends_at=timezone.now() - timedelta(days=3))
        FreeShippingRule.objects.create(title='قاعده‌ی خاموش', is_active=False)
        reset_promotions_cache()
        html = self.get('guide:shipping').content.decode()
        self.assertIn('ارسال رایگان نوروز', html)
        self.assertIn('2,000,000', html)
        self.assertIn('شهر راهنما', html)
        self.assertIn('شامل پست', html)
        self.assertNotIn('قاعده‌ی منقضی', html)
        self.assertNotIn('قاعده‌ی خاموش', html)

    def test_no_rules_no_free_shipping_table(self):
        self.assertNotIn('ارسال رایگان فعال</h2>', self.get('guide:shipping').content.decode())


class PaymentTests(GuideBase):
    def test_cheque_steps_show_the_configured_deadline_and_the_cancellation_warning(self):
        self.configure(cheque_submission_deadline_hours=36)
        html = self.get('guide:payment').content.decode()
        self.assertIn('36 ساعت', html)
        self.assertIn('سفارش خودکار لغو', html)
        self.assertIn(reverse('accounts:cheque_credit'), html)

    def test_warning_disappears_when_the_deadline_is_off(self):
        self.configure(cheque_submission_deadline_hours=0)
        self.assertNotIn('سفارش خودکار لغو', self.get('guide:payment').content.decode())

    def test_links_to_wallet_and_my_codes(self):
        html = self.get('guide:payment').content.decode()
        self.assertIn(reverse('wallet:dashboard'), html)
        self.assertIn(reverse('promotions:my_codes'), html)


class ReturnProcedureTests(GuideBase):
    def test_period_text_and_working_day_note_follow_the_settings(self):
        self.configure(return_period_days=7, return_period_unit='working_days')
        html = self.get('returns:procedure').content.decode()
        self.assertIn('7 روز کاری', html)
        self.assertIn('جمعه‌ها حساب نمی‌شوند', html)
        self.configure(return_period_days=14, return_period_unit='calendar_days')
        html = self.get('returns:procedure').content.decode()
        self.assertIn('14 روز تقویمی', html)
        self.assertNotIn('جمعه‌ها حساب نمی‌شوند', html)

    def test_attachment_limits_come_from_the_settings(self):
        self.configure(return_attachment_max_image_mb=8, return_attachment_max_video_mb=80)
        html = self.get('returns:procedure').content.decode()
        self.assertIn('تا 8 مگابایت', html)
        self.assertIn('تا 80 مگابایت', html)

    def test_reasons_show_who_pays_the_return_postage(self):
        ReturnReason.objects.create(title='کالا معیوب بود', shipping_cost_payer=ReturnReason.PAYER_STORE)
        ReturnReason.objects.create(title='نظرم عوض شد', shipping_cost_payer=ReturnReason.PAYER_CUSTOMER)
        ReturnReason.objects.create(title='دلیل غیرفعال', is_active=False)
        html = self.get('returns:procedure').content.decode()
        self.assertIn('کالا معیوب بود', html)
        self.assertRegex(html, r'کالا معیوب بود</td>\s*<td><span class="guide-badge is-store">با فروشگاه')
        self.assertRegex(html, r'نظرم عوض شد</td>\s*<td><span class="guide-badge is-customer">با مشتری')
        self.assertNotIn('دلیل غیرفعال', html)

    def test_non_returnable_rules_are_listed_with_their_targets(self):
        category = Category.objects.create(name='دسته بهداشتی راهنما', slug='guide-hygiene')
        rule = ReturnabilityRule.objects.create(scope=ReturnabilityRule.SCOPE_CATEGORY, reason_text='کالای بهداشتی قابل مرجوع نیست')
        rule.categories.add(category)
        ReturnabilityRule.objects.create(scope=ReturnabilityRule.SCOPE_PRODUCT, reason_text='قاعده‌ی خاموش', is_active=False)
        html = self.get('returns:procedure').content.decode()
        self.assertIn('کالای بهداشتی قابل مرجوع نیست', html)
        self.assertIn('دسته بهداشتی راهنما', html)
        self.assertNotIn('قاعده‌ی خاموش', html)

    def test_admin_written_policy_is_kept_as_an_extra_section(self):
        self.assertNotIn('توضیحات تکمیلی فروشگاه', self.get('returns:procedure').content.decode())
        self.configure(return_policy_html='<p>لوازم بسته‌بندی را نگه دارید.</p>')
        html = self.get('returns:procedure').content.decode()
        self.assertIn('توضیحات تکمیلی فروشگاه', html)
        self.assertIn('لوازم بسته‌بندی را نگه دارید.', html)

    def test_wizard_entry_point_is_named_like_the_real_button(self):
        self.assertIn('ثبت درخواست مرجوعی', self.get('returns:procedure').content.decode())


class HomeWhyTextTests(GuideBase):
    def test_default_text_replaces_the_lorem_ipsum(self):
        html = self.get('products:home').content.decode()
        self.assertNotIn('لورم', html)
        self.assertIn('تنوع کالا، قیمت مناسب، خرید نقدی و چکی', html)

    def test_admin_text_overrides_the_default(self):
        self.configure(home_why_text='متن دلخواه مدیر\nخط دوم')
        html = self.get('products:home').content.decode()
        self.assertIn('متن دلخواه مدیر<br>خط دوم', html)
        self.assertNotIn('تنوع کالا، قیمت مناسب', html)
