"""
کدملیِ نامعتبر: فرم‌های پروفایل رقم کنترل را می‌سنجند (وقتی SiteSettings.strict_national_code_validation روشن است)؛ مشتریِ قبلی با کدملیِ
نامعتبر مسدود نمی‌شود: بعد از ورود به ویرایش پروفایل می‌رود، سبد و گشت‌وگذار باز است و فقط ثبت سفارش تا اصلاح کدملی بسته می‌ماند.
"""
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from accounts.forms import ProfileCompleteForm, ProfileEditForm
from accounts.models import CustomUser
from accounts.testing import make_approved_user
from products.models import SiteSettings

VALID = '0499370899'          # رقم کنترل درست
INVALID = '0000000000'        # ساختگی (همه‌ی ارقام یکسان)
WRONG_CHECK = '0499370898'    # رقم کنترل غلط


class Base(TestCase):
    def setUp(self):
        self.addCleanup(cache.delete, SiteSettings.CACHE_KEY)

    def strict(self, on):
        SiteSettings.objects.update_or_create(pk=1, defaults={'strict_national_code_validation': on})
        cache.delete(SiteSettings.CACHE_KEY)

    def legacy_user(self, code=INVALID, phone='09125558001'):
        user = make_approved_user(phone)
        CustomUser.objects.filter(pk=user.pk).update(national_code=code)
        user.refresh_from_db()
        return user


class FormTests(Base):
    def setUp(self):
        super().setUp()
        self.member = make_approved_user('09125558002')

    def edit_form(self, code, user=None):
        user = user or self.member
        return ProfileEditForm({'first_name': 'علی', 'last_name': 'احمدی', 'national_code': code}, instance=user)

    def test_valid_code_is_accepted_and_digits_are_normalised(self):
        for given in (VALID, '۰۴۹۹۳۷۰۸۹۹', '٠٤٩٩٣٧٠٨٩٩'):
            with self.subTest(given=given):
                form = self.edit_form(given)
                self.assertTrue(form.is_valid(), form.errors)
                self.assertEqual(form.cleaned_data['national_code'], VALID)

    def test_invalid_codes_are_refused_with_a_clear_message(self):
        for code in (INVALID, '1111111111', WRONG_CHECK):
            with self.subTest(code=code):
                form = self.edit_form(code)
                self.assertFalse(form.is_valid())
                self.assertIn('رقم کنترلی', form.error_text)

    def test_the_complete_form_checks_too(self):
        form = ProfileCompleteForm({'first_name': 'ع', 'last_name': 'ا', 'national_code': INVALID, 'password': 'Abcd#1234x',
                                    'confirm_password': 'Abcd#1234x'}, instance=self.member)
        self.assertFalse(form.is_valid())
        self.assertIn('national_code', form.errors)

    def test_switching_strict_mode_off_goes_back_to_ten_digits_only(self):
        self.strict(False)
        self.assertTrue(self.edit_form(INVALID).is_valid())
        self.assertFalse(self.edit_form('123').is_valid())


class UserFlagTests(Base):
    def test_flag_only_for_a_filled_invalid_code_of_a_normal_user(self):
        self.assertTrue(self.legacy_user(INVALID).national_code_needs_fix)
        self.assertFalse(self.legacy_user(VALID, '09125558004').national_code_needs_fix)
        blank = CustomUser.objects.create_user(phone_number='09125558005')          # پروفایل ناقص، بدون کدملی
        self.assertFalse(blank.national_code_needs_fix)                         # ناقص بودن، «پروفایل ناقص» است

    def test_staff_are_exempt_and_the_switch_turns_it_off(self):
        admin = CustomUser.objects.create_superuser(phone_number='09125558006')
        CustomUser.objects.filter(pk=admin.pk).update(national_code=INVALID)
        admin.refresh_from_db()
        self.assertFalse(admin.national_code_needs_fix)
        user = self.legacy_user(INVALID, '09125558007')
        self.strict(False)
        self.assertFalse(user.national_code_needs_fix)


class FlowTests(Base):
    def test_login_sends_the_user_to_the_profile_page_instead_of_blocking_them(self):
        from accounts.views import _after_login_url
        user = self.legacy_user()
        request = self.client.get(reverse('products:home')).wsgi_request
        request.session = self.client.session
        self.assertEqual(_after_login_url(request, user, '/shop/'), reverse('accounts:profile'))
        good = self.legacy_user(VALID, '09125558008')
        self.assertEqual(_after_login_url(request, good, '/shop/'), '/shop/')

    def test_profile_page_explains_the_problem_and_the_user_can_still_browse(self):
        user = self.legacy_user()
        self.client.force_login(user)
        self.assertContains(self.client.get(reverse('accounts:profile')), 'data-testid="national-code-alert"')
        self.assertEqual(self.client.get(reverse('products:home')).status_code, 200)
        self.assertEqual(self.client.get(reverse('products:product_list')).status_code, 200)

    def test_checkout_is_closed_until_the_code_is_fixed_and_htmx_gets_a_client_redirect(self):
        user = self.legacy_user()
        self.client.force_login(user)
        response = self.client.get(reverse('orders:checkout'))
        self.assertRedirects(response, reverse('accounts:profile'), fetch_redirect_response=False)
        htmx = self.client.post(reverse('orders:update_invoice'), HTTP_HX_REQUEST='true')
        self.assertEqual((htmx.status_code, htmx['HX-Redirect']), (204, reverse('accounts:profile')))
        submit = self.client.post(reverse('orders:submit_order'))
        self.assertRedirects(submit, reverse('accounts:profile'), fetch_redirect_response=False)

    def test_saving_a_valid_code_reopens_everything_and_hides_the_banner(self):
        user = self.legacy_user()
        self.client.force_login(user)
        response = self.client.post(reverse('accounts:profile'), {'first_name': 'علی', 'last_name': 'احمدی', 'national_code': VALID})
        self.assertNotContains(response, 'data-testid="national-code-alert"')
        user.refresh_from_db()
        self.assertEqual(user.national_code, VALID)
        self.assertFalse(user.national_code_needs_fix)
        self.assertNotEqual(self.client.get(reverse('orders:checkout')).get('Location'), reverse('accounts:profile'))

    def test_saving_another_invalid_code_keeps_the_banner_and_the_error(self):
        user = self.legacy_user()
        self.client.force_login(user)
        response = self.client.post(reverse('accounts:profile'), {'first_name': 'علی', 'last_name': 'احمدی', 'national_code': WRONG_CHECK})
        self.assertContains(response, 'رقم کنترلی')
        self.assertContains(response, 'data-testid="national-code-alert"')
        user.refresh_from_db()
        self.assertEqual(user.national_code, INVALID)

    def test_users_with_a_valid_code_are_not_affected(self):
        user = self.legacy_user(VALID, '09125558009')
        self.client.force_login(user)
        self.assertNotContains(self.client.get(reverse('accounts:profile')), 'data-testid="national-code-alert"')
        self.assertNotEqual(self.client.get(reverse('orders:checkout')).get('Location'), reverse('accounts:profile'))
