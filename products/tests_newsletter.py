"""
خبرنامه‌ی پیامکی (products/newsletter.py): عضویت از فوتر و وبلاگ، پیام خوش‌آمد، لغو عضویت با لینک امضاشده، و ارسال گروهی از پنل.
هیچ پیامکی واقعی ارسال نمی‌شود (تسک ارسال mock است؛ فقط ردیف Notification بررسی می‌شود).
"""
import re
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from accounts.models import CustomUser
from accounts.testing import make_approved_user
from notifications.models import Notification
from products import newsletter
from products.models import NewsletterSubscriber, SiteSettings

PHONE = '09125550101'


class NewsletterBase(TestCase):
    def setUp(self):
        cache.delete(newsletter.THROTTLE_KEY.format(ip='127.0.0.1'))
        self.addCleanup(cache.delete, newsletter.THROTTLE_KEY.format(ip='127.0.0.1'))
        patcher = mock.patch('notifications.tasks.deliver_notification.delay')
        patcher.start()
        self.addCleanup(patcher.stop)

    def subscribe(self, phone=PHONE, **extra):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse('products:newsletter_subscribe'), {'phone_number': phone, **extra})

    def sms(self, key='newsletter_welcome', phone=PHONE):
        return Notification.objects.filter(template_key=key, recipient=phone)

    def unsubscribe_path(self, subscriber):
        return reverse('products:newsletter_unsubscribe', args=[newsletter.make_token(subscriber)])


class SubscribeTests(NewsletterBase):
    def test_footer_signup_stores_an_active_subscriber_and_sends_a_link_free_welcome_ending_in_lagho11(self):
        response = self.subscribe()
        self.assertContains(response, 'با موفقیت در خبرنامه ثبت شد')
        subscriber = NewsletterSubscriber.objects.get(phone_number=PHONE)
        self.assertTrue(subscriber.is_active)
        self.assertEqual(subscriber.source, 'footer')
        text = self.sms().get().text
        self.assertTrue(text.endswith('\nلغو11'))
        self.assertNotIn('http', text)               # خط پیامک متن حاوی لینک را رد می‌کند (کد ۱۴)

    def test_blog_placement_is_recorded_and_rerendered_with_its_own_wrapper_id(self):
        response = self.subscribe(placement='blog')
        self.assertContains(response, 'id="newsletter-form-wrap-blog"')
        self.assertEqual(NewsletterSubscriber.objects.get().source, 'blog')

    def test_unknown_placement_falls_back_to_footer(self):
        self.subscribe(placement='evil"><script>')
        self.assertEqual(NewsletterSubscriber.objects.get().source, 'footer')

    def test_persian_digits_and_international_format_are_accepted(self):
        self.subscribe('۰۹۱۲۵۵۵۰۱۰۱')
        self.assertTrue(NewsletterSubscriber.objects.filter(phone_number=PHONE).exists())

    def test_invalid_number_is_rejected_with_a_message_and_nothing_is_sent(self):
        response = self.subscribe('123')
        self.assertContains(response, 'شماره موبایل واردشده معتبر نیست')
        self.assertFalse(NewsletterSubscriber.objects.exists())
        self.assertFalse(Notification.objects.exists())

    def test_repeating_the_signup_does_not_resend_the_welcome(self):
        self.subscribe()
        self.subscribe()
        self.assertEqual(NewsletterSubscriber.objects.count(), 1)
        self.assertEqual(self.sms().count(), 1)

    def test_signup_throttle_per_ip(self):
        for i in range(newsletter.SUBSCRIBE_LIMIT_PER_HOUR):
            self.subscribe(f'0912555{2000 + i}')
        response = self.subscribe('09125559999')
        self.assertContains(response, 'بیش از حد مجاز')
        self.assertFalse(NewsletterSubscriber.objects.filter(phone_number='09125559999').exists())


class FormRenderingTests(NewsletterBase):
    def test_blog_page_has_one_working_phone_form_in_the_sidebar_and_no_email_input(self):
        html = self.client.get(reverse('blog:list')).content.decode()
        self.assertNotIn('type="email"', html.split('<footer')[0])
        self.assertNotIn('onsubmit="return false;"', html)
        self.assertIn('id="newsletter-form-wrap-blog"', html)
        self.assertIn('id="newsletter-form-wrap-footer"', html)                 # فوتر هم هست، ولی با شناسه‌ی جدا
        ids = re.findall(r'id="(newsletter-form-wrap[^"]*)"', html)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(html.count('name="placement" value="blog"'), 1)

    def test_phone_field_has_no_pattern_that_would_block_persian_digits(self):
        html = self.client.get(reverse('products:home')).content.decode()
        form = html[html.index('id="newsletter-form-wrap-footer"'):]
        form = form[:form.index('</form>')]
        self.assertNotIn('pattern=', form)
        self.assertIn('novalidate', form)

    def test_switching_the_newsletter_off_hides_the_blog_sidebar_box_too(self):
        SiteSettings.objects.update_or_create(pk=1, defaults={'show_newsletter': False})
        cache.delete(SiteSettings.CACHE_KEY)
        self.addCleanup(cache.delete, SiteSettings.CACHE_KEY)
        self.assertNotContains(self.client.get(reverse('blog:list')), 'newsletter-form-wrap')

    def test_footer_offers_a_link_to_unsubscribe(self):
        self.assertContains(self.client.get(reverse('products:home')), reverse('products:newsletter_unsubscribe_self'))


class UnsubscribeTests(NewsletterBase):
    def setUp(self):
        super().setUp()
        self.subscribe()
        self.subscriber = NewsletterSubscriber.objects.get(phone_number=PHONE)

    def test_get_only_asks_for_confirmation_and_does_not_unsubscribe(self):
        response = self.client.get(self.unsubscribe_path(self.subscriber))
        self.assertContains(response, 'بله، لغو عضویت')
        self.subscriber.refresh_from_db()
        self.assertTrue(self.subscriber.is_active)

    def test_post_unsubscribes_and_keeps_the_row_with_a_timestamp(self):
        response = self.client.post(self.unsubscribe_path(self.subscriber))
        self.assertContains(response, 'عضویت شما لغو شد')
        self.subscriber.refresh_from_db()
        self.assertFalse(self.subscriber.is_active)
        self.assertIsNotNone(self.subscriber.unsubscribed_at)
        self.assertContains(self.client.get(self.unsubscribe_path(self.subscriber)), 'قبلاً لغو شده')

    def test_forged_or_malformed_tokens_are_rejected_without_touching_anyone(self):
        for token in (f'{self.subscriber.pk}-0000000000000000', 'abc', '999999-' + newsletter._signature(999999), f'{self.subscriber.pk}'):
            with self.subTest(token=token):
                response = self.client.post(reverse('products:newsletter_unsubscribe', args=[token]))
                self.assertEqual(response.status_code, 404)
        self.subscriber.refresh_from_db()
        self.assertTrue(self.subscriber.is_active)

    def test_a_token_only_works_for_its_own_subscriber(self):
        other, _ = newsletter.subscribe('09125550202')
        self.client.post(self.unsubscribe_path(self.subscriber))
        other.refresh_from_db()
        self.assertTrue(other.is_active)

    def test_signing_up_again_reactivates_and_sends_a_new_welcome(self):
        self.client.post(self.unsubscribe_path(self.subscriber))
        self.subscribe()
        self.subscriber.refresh_from_db()
        self.assertTrue(self.subscriber.is_active)
        self.assertIsNone(self.subscriber.unsubscribed_at)
        self.assertEqual(self.sms().count(), 2)

    def test_logged_in_user_can_unsubscribe_their_own_number_without_a_token(self):
        user = make_approved_user(PHONE)
        self.client.force_login(user)
        url = reverse('products:newsletter_unsubscribe_self')
        self.assertContains(self.client.get(url), 'بله، لغو عضویت')
        self.assertContains(self.client.post(url), 'عضویت شما لغو شد')
        self.subscriber.refresh_from_db()
        self.assertFalse(self.subscriber.is_active)

    def test_logged_in_user_who_never_subscribed_is_told_so(self):
        self.client.force_login(make_approved_user('09125550303'))
        self.assertContains(self.client.get(reverse('products:newsletter_unsubscribe_self')), 'عضو خبرنامه نیست')

    def test_anonymous_visitor_without_a_token_is_pointed_to_the_sms_link_or_login(self):
        response = self.client.get(reverse('products:newsletter_unsubscribe_self'))
        self.assertContains(response, 'وارد حساب کاربری شوید')
        self.assertEqual(self.client.post(reverse('products:newsletter_unsubscribe_self')).status_code, 200)
        self.subscriber.refresh_from_db()
        self.assertTrue(self.subscriber.is_active)


class CampaignAdminTests(NewsletterBase):
    def setUp(self):
        super().setUp()
        self.admin_user = CustomUser.objects.create_superuser(phone_number='09125550999')
        self.client.force_login(self.admin_user)
        self.active = newsletter.subscribe('09125550401')[0]
        self.other_active = newsletter.subscribe('09125550402')[0]
        self.gone = newsletter.subscribe('09125550403')[0]
        newsletter.unsubscribe(self.gone)
        self.url = reverse('admin:products_newslettersubscriber_changelist')

    def post(self, subscribers, **extra):
        data = {'action': 'send_campaign_action', '_selected_action': [s.pk for s in subscribers], **extra}
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(self.url, data)

    def test_the_action_asks_for_a_text_and_shows_how_many_active_members_will_get_it(self):
        response = self.post([self.active, self.other_active, self.gone])
        self.assertContains(response, 'data-testid="newsletter-campaign-form"')
        self.assertContains(response, 'برای <strong>2</strong> عضو فعال')
        self.assertFalse(Notification.objects.filter(template_key='newsletter_campaign').exists())

    def test_sending_reaches_only_active_members_with_a_link_free_text_ending_in_lagho11(self):
        response = self.post([self.active, self.other_active, self.gone], apply='1', text='جشنواره‌ی پاییزه شروع شد')
        self.assertEqual(response.status_code, 302)
        messages_ = Notification.objects.filter(template_key='newsletter_campaign')
        self.assertEqual(sorted(messages_.values_list('recipient', flat=True)), ['09125550401', '09125550402'])
        mine = messages_.get(recipient='09125550401').text
        self.assertEqual(mine, 'جشنواره‌ی پاییزه شروع شد\nلغو11')

    def test_links_in_the_text_are_refused_because_the_sms_line_rejects_them(self):
        for text in ('ببینید https://mosavimart.ir/shop', 'سایت www.mosavimart.ir', 'mosavimart.ir را ببینید'):
            with self.subTest(text=text):
                self.assertContains(self.post([self.active], apply='1', text=text), 'نباید لینک')
        self.assertFalse(Notification.objects.filter(template_key='newsletter_campaign').exists())

    def test_a_hand_written_lagho11_at_the_end_is_not_doubled(self):
        for suffix in ('لغو11', ' لغو 11', '\nلغو۱۱'):
            with self.subTest(suffix=suffix):
                Notification.objects.all().delete()
                self.post([self.active], apply='1', text='متن تست' + suffix)
                self.assertEqual(Notification.objects.get(recipient='09125550401').text, 'متن تست\nلغو11')

    def test_empty_or_too_long_text_is_refused(self):
        for text, wanted in (('   ', 'متن پیام را وارد کنید'), ('الف' * 301, 'بیش از ۳۰۰ نویسه')):
            with self.subTest(wanted=wanted):
                self.assertContains(self.post([self.active], apply='1', text=text), wanted)
        self.assertFalse(Notification.objects.filter(template_key='newsletter_campaign').exists())

    def test_selecting_only_unsubscribed_members_sends_nothing(self):
        response = self.post([self.gone], apply='1', text='سلام')
        self.assertContains(response, 'هیچ عضو فعالی انتخاب نشده است')
        self.assertFalse(Notification.objects.filter(template_key='newsletter_campaign').exists())

    def test_list_shows_state_and_filters(self):
        page = self.client.get(self.url, {'is_active__exact': '0'})
        self.assertContains(page, '09125550403')
        self.assertNotContains(page, '09125550401')

    def test_templates_are_registered_for_the_admin_notification_panel(self):
        from notifications.templates_registry import TEMPLATES
        self.assertIn('newsletter_welcome', TEMPLATES)
        self.assertIn('newsletter_campaign', TEMPLATES)
