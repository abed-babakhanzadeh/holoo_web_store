"""
اصلاح‌های بازبینی امنیتی اپ accounts: آپلود آواتار، گارد سمت‌سرور تکمیل پروفایل، ذخیره‌ی پروفایل بدون بازنویسیِ تصمیم هم‌زمان مدیر،
فیلدهای دسترسی در ادمین، باطل‌شدن OTP قدیمی و سهمیه‌ی ارسال کد در درخواست‌های هم‌زمان.
"""
import io
import shutil
import tempfile
import threading
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from accounts import throttle
from accounts.forms import ProfileEditForm
from accounts.models import ApprovalStatus, CustomUser, OTPPurpose, OTPRequest, avatar_upload_path
from accounts.testing import make_approved_user, valid_national_code


def image_bytes(fmt='PNG', size=(40, 30), color=(200, 30, 30)):
    out = io.BytesIO()
    Image.new('RGB', size, color).save(out, fmt)
    return out.getvalue()


class MediaTempMixin:
    def setUp(self):
        super().setUp()
        self.media_root = tempfile.mkdtemp(prefix='avatar-test-')
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=self.media_root)
        override.enable()
        self.addCleanup(override.disable)


class AvatarUploadTests(MediaTempMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = make_approved_user('09125559001')
        self.client.force_login(self.user)
        self.url = reverse('accounts:profile_avatar')

    def upload(self, name, content, content_type='image/png'):
        return self.client.post(self.url, {'avatar': SimpleUploadedFile(name, content, content_type=content_type)}, follow=True)

    def stored(self):
        self.user.refresh_from_db()
        return self.user.avatar

    def test_an_html_file_is_refused_and_nothing_is_stored(self):
        response = self.upload('avatar.html', b'<html><script>alert(document.cookie)</script></html>', 'text/html')
        self.assertFalse(self.stored())
        self.assertFalse((Path(self.media_root) / 'avatars').exists() and any((Path(self.media_root) / 'avatars').iterdir()))
        self.assertContains(response, 'data-testid="avatar-error"')

    def test_an_html_file_disguised_with_an_image_name_and_type_is_refused_too(self):
        self.upload('avatar.png', b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', 'image/png')
        self.assertFalse(self.stored())

    def test_the_error_is_shown_once(self):
        self.upload('avatar.html', b'<html></html>', 'text/html')
        self.assertNotContains(self.client.get(reverse('accounts:profile')), 'data-testid="avatar-error"')

    def test_a_real_png_is_stored_re_encoded_under_the_user_id(self):
        self.upload('me.png', image_bytes('PNG', (1200, 900)))
        avatar = self.stored()
        self.assertEqual(avatar.name, f'avatars/{self.user.pk}.png')
        with Image.open(avatar.path) as stored:
            self.assertEqual(stored.format, 'PNG')
            self.assertLessEqual(max(stored.size), 512)

    def test_the_extension_comes_from_the_real_type_not_the_file_name(self):
        self.upload('photo.png', image_bytes('JPEG'), 'image/png')
        self.assertEqual(self.stored().name, f'avatars/{self.user.pk}.jpg')

    def test_a_polyglot_has_its_appended_payload_stripped(self):
        self.upload('x.png', image_bytes('PNG') + b'<script>alert(1)</script>')
        with open(self.stored().path, 'rb') as handle:
            self.assertNotIn(b'<script', handle.read())

    def test_an_oversized_file_is_refused(self):
        self.upload('big.png', image_bytes('PNG') + b'0' * (2 * 1024 * 1024 + 10))
        self.assertFalse(self.stored())

    def test_a_corrupt_image_is_refused(self):
        self.upload('bad.png', b'\x89PNG\r\n\x1a\n' + b'not really a png')
        self.assertFalse(self.stored())

    def test_the_stored_extension_can_never_be_html_even_through_the_model_path(self):
        user = self.user
        for name in ('x.html', 'x.svg', 'x.php', 'x', 'x.JPEG'):
            self.assertRegex(avatar_upload_path(user, name), r'^avatars/\d+\.(jpg|png|webp)$')
        self.assertEqual(avatar_upload_path(user, 'x.html'), f'avatars/{user.pk}.jpg')


class ProfileCompleteGuardTests(TestCase):
    """ POST مستقیم تکمیل پروفایل برای کاربرِ کامل نباید هویت/رمز را بی‌کنترل عوض کند. """

    def setUp(self):
        self.user = make_approved_user('09125559011', first_name='علی', last_name='احمدی')
        self.user.set_password('Old#Pass123')
        self.user.save()
        self.client.force_login(self.user)
        self.data = {'first_name': 'هکر', 'last_name': 'جدید', 'national_code': valid_national_code(777000001),
                     'password': 'Evil#Pass12345', 'confirm_password': 'Evil#Pass12345'}

    def test_a_complete_user_cannot_change_identity_or_password_through_this_endpoint(self):
        before = CustomUser.objects.get(pk=self.user.pk)
        response = self.client.post(reverse('accounts:profile_complete'), self.data, HTTP_HX_REQUEST='true')
        self.assertEqual(response.status_code, 200)
        self.assertIn('HX-Redirect', response)
        after = CustomUser.objects.get(pk=self.user.pk)
        self.assertEqual((after.first_name, after.last_name, after.national_code), (before.first_name, before.last_name, before.national_code))
        self.assertTrue(after.check_password('Old#Pass123'))
        self.assertFalse(after.check_password('Evil#Pass12345'))
        self.assertEqual((after.approval_status, after.status), (ApprovalStatus.APPROVED, before.status))

    def test_a_user_with_an_incomplete_profile_can_still_complete_it(self):
        fresh = CustomUser.objects.create_user(phone_number='09125559012')
        self.client.force_login(fresh)
        self.client.post(reverse('accounts:profile_complete'), self.data, HTTP_HX_REQUEST='true')
        fresh.refresh_from_db()
        self.assertEqual((fresh.first_name, fresh.last_name), ('هکر', 'جدید'))
        self.assertTrue(fresh.check_password('Evil#Pass12345'))

    def test_resubmitting_the_completion_form_is_harmless(self):
        fresh = CustomUser.objects.create_user(phone_number='09125559013')
        self.client.force_login(fresh)
        url = reverse('accounts:profile_complete')
        self.client.post(url, self.data, HTTP_HX_REQUEST='true')
        again = {**self.data, 'first_name': 'تغییر', 'password': 'Other#Pass12345', 'confirm_password': 'Other#Pass12345'}
        response = self.client.post(url, again, HTTP_HX_REQUEST='true')
        self.assertEqual(response.status_code, 200)
        fresh.refresh_from_db()
        self.assertEqual(fresh.first_name, 'هکر')
        self.assertTrue(fresh.check_password('Evil#Pass12345'))


class ProfileSaveLostUpdateTests(TestCase):
    """ مدیر بین «بارگذاری کاربر» و «ذخیره‌ی پروفایل» تصمیم می‌گیرد؛ ذخیره‌ی پروفایل نباید آن را برگرداند. """

    def setUp(self):
        self.user = make_approved_user('09125559021', first_name='علی', last_name='احمدی', cheque_credit_limit=Decimal('5000000'),
                                       can_purchase_with_check=True)
        self.client.force_login(self.user)
        self.url = reverse('accounts:profile')
        self.data = {'first_name': 'علی', 'last_name': 'احمدی', 'national_code': self.user.national_code, 'email': 'a@example.com'}

    def post_with_admin_decision(self, decision):
        real_is_valid = ProfileEditForm.is_valid

        def is_valid_then_admin_acts(form):
            result = real_is_valid(form)
            CustomUser.objects.filter(pk=self.user.pk).update(**decision)           # مدیر درست بعد از بارگذاری کاربر
            return result

        with mock.patch.object(ProfileEditForm, 'is_valid', autospec=True, side_effect=is_valid_then_admin_acts):
            return self.client.post(self.url, self.data)

    def test_a_credit_freeze_made_meanwhile_survives_the_profile_save(self):
        self.post_with_admin_decision({'cheque_credit_limit': Decimal('0'), 'can_purchase_with_check': False})
        after = CustomUser.objects.get(pk=self.user.pk)
        self.assertEqual(after.cheque_credit_limit, Decimal('0'))
        self.assertFalse(after.can_purchase_with_check)
        self.assertEqual(after.email, 'a@example.com')                          # ولی ویرایش خودِ کاربر ذخیره شد

    def test_a_rejection_made_meanwhile_survives_the_profile_save(self):
        self.post_with_admin_decision({'approval_status': ApprovalStatus.REJECTED, 'rejection_reason': 'مدارک ناقص'})
        after = CustomUser.objects.get(pk=self.user.pk)
        self.assertEqual((after.approval_status, after.rejection_reason), (ApprovalStatus.REJECTED, 'مدارک ناقص'))

    def test_a_block_made_meanwhile_survives_the_profile_save(self):
        self.post_with_admin_decision({'is_active': False})
        self.assertFalse(CustomUser.objects.get(pk=self.user.pk).is_active)

    def test_a_normal_edit_still_saves_and_identity_change_still_revokes_approval(self):
        self.client.post(self.url, {**self.data, 'first_name': 'محمد'})
        after = CustomUser.objects.get(pk=self.user.pk)
        self.assertEqual(after.first_name, 'محمد')
        self.assertEqual(after.approval_status, ApprovalStatus.PENDING)


class AdminPrivilegeTests(TestCase):
    """ کارمندِ دارای مجوز «تغییر کاربر» نباید بتواند خودش یا دیگری را مدیرکل کند. """

    def setUp(self):
        self.operator = CustomUser.objects.create_user(phone_number='09125559031', password='x', is_staff=True)
        for codename in ('view_customuser', 'change_customuser'):
            self.operator.user_permissions.add(Permission.objects.get(content_type__app_label='accounts', codename=codename))
        self.customer = make_approved_user('09125559032')
        self.boss = CustomUser.objects.create_superuser(phone_number='09125559033', password='x')

    def change_url(self, user):
        return reverse('admin:accounts_customuser_change', args=[user.pk])

    def post_data(self, user, **extra):
        return {'phone_number': user.phone_number, 'status': user.status, 'first_name': user.first_name or '', 'last_name': user.last_name or '',
                'national_code': user.national_code or '', 'price_level': user.price_level, 'is_active': 'on', **extra,
                'addresses-TOTAL_FORMS': 0, 'addresses-INITIAL_FORMS': 0, 'addresses-MIN_NUM_FORMS': 0, 'addresses-MAX_NUM_FORMS': 1000,
                'bank_accounts-TOTAL_FORMS': 0, 'bank_accounts-INITIAL_FORMS': 0, 'bank_accounts-MIN_NUM_FORMS': 0, 'bank_accounts-MAX_NUM_FORMS': 1000}

    def test_the_operator_cannot_open_or_change_their_own_staff_account(self):
        self.client.force_login(self.operator)
        self.assertEqual(self.client.post(self.change_url(self.operator), self.post_data(self.operator, is_superuser='on')).status_code, 403)
        self.operator.refresh_from_db()
        self.assertFalse(self.operator.is_superuser)

    def test_the_operator_cannot_touch_a_superuser(self):
        self.client.force_login(self.operator)
        self.assertEqual(self.client.post(self.change_url(self.boss), self.post_data(self.boss, is_active='')).status_code, 403)
        self.assertTrue(CustomUser.objects.get(pk=self.boss.pk).is_active)

    def test_the_operator_cannot_promote_a_customer_by_posting_privilege_fields(self):
        self.client.force_login(self.operator)
        response = self.client.get(self.change_url(self.customer))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('is_superuser', response.context['adminform'].form.fields)
        self.assertNotIn('is_staff', response.context['adminform'].form.fields)
        self.client.post(self.change_url(self.customer), self.post_data(self.customer, is_superuser='on', is_staff='on'))
        after = CustomUser.objects.get(pk=self.customer.pk)
        self.assertFalse(after.is_superuser or after.is_staff)

    def test_the_operator_can_still_edit_a_normal_customer(self):
        self.client.force_login(self.operator)
        self.client.post(self.change_url(self.customer), self.post_data(self.customer, business_name='فروشگاه نمونه'))
        self.assertEqual(CustomUser.objects.get(pk=self.customer.pk).business_name, 'فروشگاه نمونه')

    def test_a_superuser_keeps_full_control(self):
        self.client.force_login(self.boss)
        response = self.client.get(self.change_url(self.operator))
        self.assertEqual(response.status_code, 200)
        self.assertIn('is_superuser', response.context['adminform'].form.fields)
        self.client.post(self.change_url(self.customer), self.post_data(self.customer, is_staff='on'))
        self.assertTrue(CustomUser.objects.get(pk=self.customer.pk).is_staff)


class OtpInvalidationTests(TestCase):
    phone = '09125559041'

    def test_issuing_a_new_code_voids_the_older_unused_ones(self):
        old = OTPRequest.issue(self.phone, OTPPurpose.REGISTER_LOGIN, '1.1.1.1')
        new = OTPRequest.issue(self.phone, OTPPurpose.REGISTER_LOGIN, '1.1.1.1')
        self.assertEqual(OTPRequest.objects.filter(phone_number=self.phone, used_at__isnull=True).count(), 1)
        self.assertTrue(OTPRequest.verify_code(self.phone, new)[0])

    def test_the_old_code_cannot_be_used_after_the_new_one_was_consumed(self):
        """ بازتولید ایراد: دو کد، مصرفِ جدید، سپس قدیمی (هنوز در مهلت) نباید ورود بدهد """
        OTPRequest.objects.create(phone_number=self.phone, code='111111', purpose=OTPPurpose.REGISTER_LOGIN,
                                  expires_at=timezone.now() + timedelta(minutes=2))
        OTPRequest.objects.create(phone_number=self.phone, code='222222', purpose=OTPPurpose.REGISTER_LOGIN,
                                  expires_at=timezone.now() + timedelta(minutes=2))
        self.assertTrue(OTPRequest.verify_code(self.phone, '222222')[0])
        self.assertFalse(OTPRequest.verify_code(self.phone, '111111')[0])

    def test_codes_of_another_purpose_or_phone_are_untouched(self):
        other_purpose = OTPRequest.issue(self.phone, OTPPurpose.RESET_PASSWORD, '1.1.1.1')
        other_phone = OTPRequest.issue('09125559042', OTPPurpose.REGISTER_LOGIN, '1.1.1.1')
        OTPRequest.issue(self.phone, OTPPurpose.REGISTER_LOGIN, '1.1.1.1')
        self.assertTrue(OTPRequest.verify_code(self.phone, other_purpose, purpose=OTPPurpose.RESET_PASSWORD)[0])
        self.assertTrue(OTPRequest.verify_code('09125559042', other_phone)[0])


class OtpQuotaTests(SimpleTestCase):
    phone, ip = '09125559051', '203.0.113.5'

    def setUp(self):
        self.clear()
        self.addCleanup(self.clear)

    def clear(self):
        cache.delete_many([f'otp:cooldown:{self.phone}', f'otp:phone:{self.phone}', f'otp:ip:{self.ip}'])

    def test_only_one_of_many_simultaneous_requests_gets_through(self):
        """ بازتولید ایراد: بررسی و مصرف جدا بود؛ هر ۲۰ درخواست هم‌زمان از بررسی رد می‌شدند """
        results, barrier = [], threading.Barrier(20)

        def attempt():
            barrier.wait()
            try:
                throttle.reserve_otp_quota(self.phone, self.ip)
                results.append('ok')
            except throttle.ThrottleError:
                results.append('blocked')

        threads = [threading.Thread(target=attempt) for _ in range(20)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(results.count('ok'), 1)
        self.assertEqual(cache.get(f'otp:phone:{self.phone}'), 1)

    def test_hourly_caps_hold_and_rejections_do_not_inflate_the_counters(self):
        granted = 0
        for _ in range(throttle.MAX_PER_PHONE_PER_HOUR + 4):
            cache.delete(f'otp:cooldown:{self.phone}')                              # فاصله‌ی حداقلی را کنار می‌گذاریم تا سقف ساعتی سنجیده شود
            try:
                throttle.reserve_otp_quota(self.phone, self.ip)
                granted += 1
            except throttle.ThrottleError:
                pass
        self.assertEqual(granted, throttle.MAX_PER_PHONE_PER_HOUR)
        self.assertEqual(cache.get(f'otp:phone:{self.phone}'), throttle.MAX_PER_PHONE_PER_HOUR)

    def test_the_cooldown_message_and_release(self):
        throttle.reserve_otp_quota(self.phone, self.ip)
        with self.assertRaisesMessage(throttle.ThrottleError, 'صبر کنید'):
            throttle.reserve_otp_quota(self.phone, self.ip)
        throttle.release_otp_quota(self.phone, self.ip)
        throttle.reserve_otp_quota(self.phone, self.ip)                             # بعد از release دوباره ممکن است
        self.assertEqual(cache.get(f'otp:phone:{self.phone}'), 1)


class SendOtpViewQuotaTests(TestCase):
    phone = '09125559061'

    def setUp(self):
        self.keys = [f'otp:cooldown:{self.phone}', f'otp:phone:{self.phone}', 'otp:ip:127.0.0.1']
        cache.delete_many(self.keys)
        self.addCleanup(cache.delete_many, self.keys)

    def test_a_successful_send_consumes_the_quota_and_the_second_immediate_request_is_blocked(self):
        with mock.patch('accounts.views.notify') as notify:
            self.client.post(reverse('accounts:send_otp'), {'phone_number': self.phone})
            second = self.client.post(reverse('accounts:send_otp'), {'phone_number': self.phone})
        self.assertEqual(notify.call_count, 1)
        self.assertContains(second, 'صبر کنید')

    def test_a_failed_send_gives_the_quota_back(self):
        with mock.patch('accounts.views.notify', side_effect=RuntimeError('sms down')):
            with self.assertRaises(RuntimeError):
                self.client.post(reverse('accounts:send_otp'), {'phone_number': self.phone})
        self.assertIsNone(cache.get(f'otp:cooldown:{self.phone}'))
        self.assertFalse(cache.get(f'otp:phone:{self.phone}'))
