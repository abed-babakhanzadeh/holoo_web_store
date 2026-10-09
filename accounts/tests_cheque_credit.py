"""
فاز F1 درخواست خرید چکی / اعتباری: مدل‌ها و قیدها، سرویس ثبت (شرایط، اعتبارسنجی، تصاویر امن، هم‌زمانی)، انصراف، تأیید (روشن‌شدن
can_purchase_with_check)، رد (علت الزامی)، سیگنال‌ها پس از commit و پاک‌سازی مدارک.
MEDIA_ROOT هر تست یک پوشه‌ی موقت است؛ هیچ فایلی به media/ واقعی نمی‌رود.
"""
import os
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from accounts import cheque_credit_service as service
from accounts.cheque_credit import ChequeCreditDocument, ChequeCreditRequest
from accounts.cheque_credit_service import ChequeCreditError
from accounts.models import CustomUser
from accounts.signals import cheque_credit_approved, cheque_credit_rejected, cheque_credit_requested
from accounts.testing import make_approved_user
from orders import payment_options
from orders.tests_cheques import MediaIsolation, make_image, upload
from orders.tests_payment_options import set_policy
from products.models import SiteSettings
from products.pricing import VIP_CHEQUE_STANDARD_PRICE, VIP_CHEQUE_DISABLED, VIP_CHEQUE_REQUEST


_code_seq = iter(range(100000000, 999999999))


def valid_national_code():
    """ کد ملی ۱۰ رقمی با رقم کنترل درست (الگوریتم رسمی) """
    base = f'{next(_code_seq):09d}'
    remainder = sum(int(base[i]) * (10 - i) for i in range(9)) % 11
    return base + str(remainder if remainder < 2 else 11 - remainder)


def make_user(phone, **extra):
    extra.setdefault('national_code', valid_national_code())
    return make_approved_user(phone, **extra)


def set_strict(value):
    settings_obj = SiteSettings.load()
    settings_obj.strict_national_code_validation = value
    settings_obj.save()
    cache.delete(SiteSettings.CACHE_KEY)


def make_iban(bban='0170000000123456789012'):
    check = 98 - (int(bban + '1827' + '00') % 97)
    return f'IR{check:02d}{bban}'


IBAN = make_iban()
OTHER_IBAN = make_iban('0620000000987654321098')


def form_data(**overrides):
    data = {'business_name': 'فروشگاه نمونه', 'bank_name': 'ملی', 'account_holder': 'علی رضایی', 'iban': IBAN,
            'requested_limit': '50,000,000', 'monthly_turnover': '', 'description': ''}
    data.update(overrides)
    return data


def docs(*kinds):
    return [(kind, upload(make_image())) for kind in (kinds or ('cheque_book',))]


def set_retention(days):
    settings_obj = SiteSettings.load()
    settings_obj.cheque_credit_docs_retention_days = days
    settings_obj.save()
    cache.delete(SiteSettings.CACHE_KEY)


def stored(root):
    found = []
    for folder, _, names in os.walk(os.path.join(root, 'cheque_credit_docs')):
        found += [os.path.join(folder, n) for n in names]
    return found


class CreditBase(MediaIsolation, TestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user('09120000501', price_level=2, first_name='علی', last_name='رضایی')
        self.admin = make_user('09120000502', is_staff=True, is_superuser=True)

    def submit(self, user=None, data=None, files=None):
        with self.captureOnCommitCallbacks(execute=True):
            return service.submit_request(user or self.user, data or form_data(), files if files is not None else docs())

    def errors(self, func, *args, **kwargs):
        with self.assertRaises(ChequeCreditError) as caught:
            func(*args, **kwargs)
        return caught.exception

    def reload(self, obj):
        return type(obj).objects.get(pk=obj.pk)

    def listen(self, signal):
        seen = []
        handler = lambda sender, request, **kw: seen.append((request.pk, request.status))     # noqa: E731
        signal.connect(handler, weak=False, dispatch_uid=f'credit_test_{id(seen)}')
        self.addCleanup(signal.disconnect, dispatch_uid=f'credit_test_{id(seen)}')
        return seen


# ------------------------------------------------------------------ مدل و قیدها
class ModelConstraintTests(CreditBase):
    def make(self, **fields):
        data = dict(user=self.user, first_name='علی', last_name='رضایی', national_code='1234567890', business_name='ف',
                    bank_name='ملی', account_holder='ع', iban=IBAN, requested_limit=1000)
        data.update(fields)
        return ChequeCreditRequest.objects.create(**data)

    def test_a_second_pending_request_for_one_user_is_refused_by_the_database(self):
        self.make()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make()

    def test_a_decided_request_does_not_block_a_new_pending_one(self):
        self.make(status='rejected', rejection_reason='x')
        self.make(status='canceled')
        self.make()
        self.assertEqual(ChequeCreditRequest.objects.filter(user=self.user).count(), 3)

    def test_another_user_can_have_a_pending_request_at_the_same_time(self):
        self.make()
        other = make_user('09120000503', price_level=2)
        self.make(user=other)

    def test_rejection_without_a_reason_is_refused_by_the_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make(status='rejected', rejection_reason='')

    def test_a_non_positive_limit_is_refused_by_the_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make(requested_limit=0)

    def test_the_user_cannot_be_deleted_while_requests_exist(self):
        from django.db.models import ProtectedError
        self.make()
        with self.assertRaises(ProtectedError):
            self.user.delete()

    def test_the_retention_setting_defaults_to_30_days_and_is_bounded(self):
        self.assertEqual(SiteSettings.load().cheque_credit_docs_retention_days, 30)
        obj = SiteSettings.load()
        exclude = [f.name for f in obj._meta.fields if f.name != 'cheque_credit_docs_retention_days']
        obj.cheque_credit_docs_retention_days = 3651
        with self.assertRaises(ValidationError):
            obj.full_clean(exclude=exclude)
        obj.cheque_credit_docs_retention_days = 0
        obj.full_clean(exclude=exclude)

    def test_the_retention_setting_is_editable_in_the_site_settings_admin(self):
        from django.contrib.admin.sites import site
        fields = [f for _, opts in site._registry[SiteSettings].get_fieldsets(None) for f in opts['fields']]
        self.assertIn('cheque_credit_docs_retention_days', fields)


# ------------------------------------------------------------------ شرایط ثبت
class EligibilityTests(CreditBase):
    def test_a_cash_customer_may_request(self):
        self.assertIsNone(service.eligibility_blocker(self.user))

    def test_a_cheque_customer_does_not_need_to_request(self):
        user = make_user('09120000510', price_level=1)
        self.assertIsNotNone(service.eligibility_blocker(user))

    def test_a_vip_customer_follows_the_vip_policy(self):
        vip = make_user('09120000511', price_level=3)
        set_policy(VIP_CHEQUE_DISABLED)
        self.assertIn('فعال نیست', service.eligibility_blocker(vip))
        set_policy(VIP_CHEQUE_STANDARD_PRICE)                       # چکیِ مستقیم دارد؛ درخواست لازم نیست
        self.assertIsNotNone(service.eligibility_blocker(vip))
        set_policy(VIP_CHEQUE_REQUEST)
        self.assertIsNone(service.eligibility_blocker(vip))

    def test_a_user_who_already_has_the_permission_is_refused(self):
        CustomUser.objects.filter(pk=self.user.pk).update(can_purchase_with_check=True)
        self.assertIn('فعال است', service.eligibility_blocker(self.reload(self.user)))

    def test_an_unapproved_user_is_refused(self):
        pending = CustomUser.objects.create_user(phone_number='09120000512', first_name='الف', last_name='ب', national_code='1234567890')
        self.assertIn('تأیید نشده', service.eligibility_blocker(pending))

    def test_an_incomplete_profile_is_refused(self):
        CustomUser.objects.filter(pk=self.user.pk).update(national_code='123')
        self.assertIn('پروفایل', service.eligibility_blocker(self.reload(self.user)))

    def test_anonymous_and_none_are_refused(self):
        from django.contrib.auth.models import AnonymousUser
        self.assertIsNotNone(service.eligibility_blocker(AnonymousUser()))
        self.assertIsNotNone(service.eligibility_blocker(None))

    def test_submit_refuses_an_ineligible_user_with_409(self):
        user = make_user('09120000513', price_level=1)
        error = self.errors(service.submit_request, user, form_data(), docs())
        self.assertEqual(error.status, 409)
        self.assertEqual(ChequeCreditRequest.objects.count(), 0)


# ------------------------------------------------------------------ ثبت
class SubmitTests(CreditBase):
    def test_a_valid_request_is_stored_with_a_profile_snapshot_and_clean_documents(self):
        request = self.submit(files=docs('cheque_book', 'national_card'))
        self.assertEqual(request.status, 'pending')
        self.assertEqual((request.first_name, request.last_name, request.national_code),
                         ('علی', 'رضایی', self.user.national_code))
        self.assertEqual((request.business_name, request.bank_name, request.iban), ('فروشگاه نمونه', 'ملی', IBAN))
        self.assertEqual(int(request.requested_limit), 50000000)
        self.assertIsNone(request.monthly_turnover)
        documents = list(request.documents.all())
        self.assertEqual(sorted(d.kind for d in documents), ['cheque_book', 'national_card'])
        files = stored(self.media_root)
        self.assertEqual(len(files), 2)
        for doc in documents:
            self.assertTrue(doc.file.name.endswith(f'{doc.public_id}.jpg'))        # نام uuid، نه نام کاربر
            self.assertNotIn('cheque.jpg', doc.file.name)
            with self.assertRaises(ValueError):
                doc.file.url                                                        # نشانی عمومی ندارد

    def test_identity_comes_from_the_profile_not_the_form(self):
        request = self.submit(data=form_data(first_name='جعلی', last_name='جعلی', national_code='0000000000'))
        self.assertEqual((request.first_name, request.last_name), ('علی', 'رضایی'))

    def test_text_numbers_and_iban_are_normalized(self):
        request = self.submit(data=form_data(
            requested_limit='۵۰٬۰۰۰٬۰۰۰', monthly_turnover='1,200,000', iban=' ' + IBAN[:6] + ' ' + IBAN[6:],
            description='  سلام \n\x00 دنیا  ', business_name='  فروشگاه   من '))
        self.assertEqual(int(request.requested_limit), 50000000)
        self.assertEqual(int(request.monthly_turnover), 1200000)
        self.assertEqual(request.iban, IBAN)
        self.assertEqual(request.description, 'سلام دنیا')
        self.assertEqual(request.business_name, 'فروشگاه من')

    def test_the_iban_may_be_given_without_the_ir_prefix_or_with_persian_digits(self):
        for raw in (IBAN[2:], IBAN.lower(), IBAN[:2] + ' ' + IBAN[2:4] + '-' + IBAN[4:]):
            with self.subTest(raw=raw):
                self.assertEqual(service.normalize_iban(raw), IBAN)
        persian = IBAN.translate(str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹'))
        self.assertEqual(service.normalize_iban(persian), IBAN)

    def test_bad_ibans_are_refused(self):
        for bad in ('', '123', IBAN + '1', 'IR' + 'x' * 24, IBAN[:-1] + str((int(IBAN[-1]) + 1) % 10)):         # آخری: رقم کنترل نمی‌خواند
            with self.subTest(bad=bad):
                self.assertIn('iban', self.errors(service.normalize_iban, bad).errors)

    def test_every_field_error_is_reported_at_once_and_nothing_is_saved(self):
        error = self.errors(service.submit_request, self.user,
                            form_data(business_name='', bank_name='', account_holder='', iban='1', requested_limit='abc',
                                      monthly_turnover='-5', description='x' * 1001),
                            [])
        self.assertEqual(set(error.errors), {'business_name', 'bank_name', 'account_holder', 'iban', 'requested_limit',
                                             'monthly_turnover', 'description', 'documents'})
        self.assertEqual(ChequeCreditRequest.objects.count(), 0)
        self.assertEqual(stored(self.media_root), [])

    def test_amounts_must_be_positive_and_bounded(self):
        for bad in ('0', '-1', '1.5', 'abc', str(10 ** 13)):
            with self.subTest(bad=bad):
                self.assertIn('requested_limit', self.errors(service.parse_amount, bad, 'requested_limit', 'سقف', required=True).errors)
        self.assertIsNone(service.parse_amount('', 'monthly_turnover', 'گردش'))

    def test_a_cheque_book_image_is_required(self):
        for files in ([], docs('national_card'), docs('other', 'business_license')):
            with self.subTest(kinds=[k for k, _ in files]):
                self.assertIn('تصویر دسته‌چک', self.errors(service.submit_request, self.user, form_data(), files).errors['documents'])

    def test_at_most_five_documents(self):
        files = docs('cheque_book', 'national_card', 'business_license', 'other', 'other', 'other')
        self.assertIn('حداکثر', self.errors(service.submit_request, self.user, form_data(), files).errors['documents'])
        self.assertEqual(len(self.submit(files=docs('cheque_book', 'national_card', 'business_license', 'other', 'other')).documents.all()), 5)

    def test_an_unknown_kind_is_refused(self):
        files = docs('cheque_book') + [('passport', upload(make_image()))]
        self.assertIn('documents', self.errors(service.submit_request, self.user, form_data(), files).errors)

    def test_only_real_jpg_png_webp_images_are_accepted(self):
        pdf = [('cheque_book', upload(b'%PDF-1.4 fake', 'x.pdf', 'application/pdf'))]
        self.assertIn('PDF', self.errors(service.submit_request, self.user, form_data(), pdf).errors['documents'])
        spoofed = [('cheque_book', upload(b'<?php echo 1;', 'shell.jpg', 'image/jpeg'))]
        self.assertIn('documents', self.errors(service.submit_request, self.user, form_data(), spoofed).errors)
        corrupt = [('cheque_book', upload(make_image()[:40], 'a.jpg', 'image/jpeg'))]
        self.assertIn('documents', self.errors(service.submit_request, self.user, form_data(), corrupt).errors)
        for phone, fmt, ctype, name in (('09120000551', 'PNG', 'image/png', 'a.png'), ('09120000552', 'WEBP', 'image/webp', 'a.webp')):
            with self.subTest(fmt=fmt):
                user = make_user(phone, price_level=2)
                request = service.submit_request(user, form_data(), [('cheque_book', upload(make_image(fmt), name, ctype))])
                self.assertEqual(request.documents.get().content_type, ctype)

    def test_oversized_images_are_refused(self):
        big = upload(make_image(), 'big.jpg')
        big.size = service.MAX_DOC_MB * 1024 * 1024 + 1
        self.assertIn('مگابایت', self.errors(service.submit_request, self.user, form_data(), [('cheque_book', big)]).errors['documents'])

    def test_exif_and_trailing_data_are_stripped(self):
        request = self.submit(files=[('cheque_book', upload(make_image(exif=True, trailing=b'<script>x</script>')))])
        doc = request.documents.get()
        with doc.file.open('rb') as handle:
            data = handle.read()
        self.assertNotIn(b'SECRET-GPS-123', data)
        self.assertNotIn(b'<script>', data)

    def test_a_second_pending_request_is_refused(self):
        self.submit()
        error = self.errors(service.submit_request, self.user, form_data(), docs())
        self.assertEqual(error.status, 409)
        self.assertIn('در انتظار بررسی', error.message)
        self.assertEqual(ChequeCreditRequest.objects.count(), 1)

    def test_another_user_is_independent(self):
        self.submit()
        other = make_user('09120000520', price_level=2)
        self.submit(user=other)
        self.assertEqual(ChequeCreditRequest.objects.count(), 2)

    def test_resubmitting_after_a_rejection_or_cancel_is_allowed_without_a_wait(self):
        first = self.submit()
        service.reject_request(first, self.admin, 'ناخوانا')
        second = self.submit()
        service.cancel_request(second, self.user)
        third = self.submit()
        self.assertEqual([r.status for r in ChequeCreditRequest.objects.order_by('id')], ['rejected', 'canceled', 'pending'])
        self.assertEqual(third.status, 'pending')

    def test_the_database_guard_turns_a_lost_race_into_a_409_and_leaves_no_files(self):
        self.submit()
        before = set(stored(self.media_root))
        with mock.patch('accounts.cheque_credit_service.pending_request', return_value=None):      # هر دو پیش‌بررسی را رد کردند
            error = self.errors(service.submit_request, self.user, form_data(), docs())
        self.assertEqual(error.status, 409)
        self.assertEqual(set(stored(self.media_root)), before)
        self.assertEqual(ChequeCreditRequest.objects.count(), 1)

    def test_a_failure_midway_leaves_no_request_and_no_files(self):
        real_save = ChequeCreditDocument.save
        calls = {'n': 0}

        def flaky(doc, *args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 2:
                raise RuntimeError('boom')
            return real_save(doc, *args, **kwargs)
        with mock.patch.object(ChequeCreditDocument, 'save', flaky):
            with self.assertRaises(RuntimeError):
                service.submit_request(self.user, form_data(), docs('cheque_book', 'national_card'))
        self.assertEqual(ChequeCreditRequest.objects.count(), 0)
        self.assertEqual(stored(self.media_root), [])

    def test_the_rate_limit_applies_after_five_attempts(self):
        for _ in range(service.RATE_LIMIT):
            self.errors(service.submit_request, self.user, form_data(iban='1'), docs())
        error = self.errors(service.submit_request, self.user, form_data(), docs())
        self.assertEqual(error.status, 429)
        self.assertEqual(ChequeCreditRequest.objects.count(), 0)

    def test_the_rate_limit_is_checked_before_the_expensive_decode(self):
        with mock.patch('accounts.cheque_credit_service._rate_limited', return_value=True), \
                mock.patch('accounts.cheque_credit_service.clean_image') as decode:
            self.errors(service.submit_request, self.user, form_data(), docs())
        decode.assert_not_called()

    def test_the_requested_signal_fires_only_after_commit(self):
        seen = self.listen(cheque_credit_requested)
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            request = service.submit_request(self.user, form_data(), docs())
        self.assertEqual(seen, [])
        for callback in callbacks:
            callback()
        self.assertEqual(seen, [(request.pk, 'pending')])

    def test_tests_never_touch_the_real_media_folder(self):
        from django.conf import settings
        self.submit()
        self.assertTrue(str(settings.MEDIA_ROOT).startswith(str(self.media_root)))
        self.assertTrue(all(path.startswith(str(self.media_root)) for path in stored(self.media_root)))


# ------------------------------------------------------------------ انصراف
class CancelTests(CreditBase):
    def test_the_owner_can_cancel_a_pending_request(self):
        request = self.submit()
        canceled = service.cancel_request(request, self.user)
        self.assertEqual(canceled.status, 'canceled')
        self.assertIsNotNone(self.reload(request).decided_at)
        self.assertFalse(self.reload(self.user).can_purchase_with_check)

    def test_someone_elses_request_is_a_404(self):
        request = self.submit()
        other = make_user('09120000530', price_level=2)
        self.assertEqual(self.errors(service.cancel_request, request, other).status, 404)
        self.assertEqual(self.reload(request).status, 'pending')

    def test_only_a_pending_request_can_be_canceled(self):
        request = self.submit()
        service.reject_request(request, self.admin, 'x')
        self.assertEqual(self.errors(service.cancel_request, request, self.user).status, 409)
        self.assertEqual(self.reload(request).status, 'rejected')

    def test_a_stale_object_cannot_override_a_decision(self):
        request = self.submit()
        stale = ChequeCreditRequest.objects.get(pk=request.pk)
        service.approve_request(request, self.admin)
        self.assertEqual(self.errors(service.cancel_request, stale, self.user).status, 409)
        self.assertEqual(self.reload(request).status, 'approved')


# ------------------------------------------------------------------ تأیید
class ApproveTests(CreditBase):
    def approve(self, request, **kwargs):
        with self.captureOnCommitCallbacks(execute=True):
            return service.approve_request(request, self.admin, **kwargs)

    def test_approval_turns_the_permission_on_and_stamps_the_review(self):
        request = self.submit()
        self.assertFalse(self.reload(self.user).can_purchase_with_check)
        approved = self.approve(request, approved_limit='30,000,000', admin_note='  سابقه‌ی خوب ')
        self.assertEqual(approved.status, 'approved')
        self.assertEqual((approved.reviewed_by, int(approved.approved_limit), approved.admin_note), (self.admin, 30000000, 'سابقه‌ی خوب'))
        self.assertIsNotNone(approved.reviewed_at)
        self.assertEqual(approved.decided_at, approved.reviewed_at)
        self.assertTrue(self.reload(self.user).can_purchase_with_check)

    def test_the_price_level_and_other_user_fields_are_untouched(self):
        before = CustomUser.objects.filter(pk=self.user.pk).values().get()
        self.approve(self.submit())
        after = CustomUser.objects.filter(pk=self.user.pk).values().get()
        changed = {key for key in before if before[key] != after[key]}
        self.assertEqual(changed, {'can_purchase_with_check', 'cheque_credit_limit'})

    def test_the_approved_customer_now_sees_the_cheque_option(self):
        self.assertEqual([o.key for o in payment_options.order_options(self.user)], ['cash'])
        self.approve(self.submit())
        user = self.reload(self.user)
        self.assertIn('check', [o.key for o in payment_options.order_options(user)])
        self.assertIsNone(payment_options.request_option(user))
        self.assertIn('فعال است', service.eligibility_blocker(user))

    def test_the_limit_is_optional_and_validated(self):
        request = self.submit()
        self.assertEqual(self.errors(service.approve_request, request, self.admin, approved_limit='abc').errors.keys(), {'approved_limit'})
        self.assertEqual(self.reload(request).status, 'pending')
        approved = self.approve(request)
        self.assertEqual(int(approved.approved_limit), 50000000)                    # بدون مقدار: همان سقف درخواستی (G1)
        self.assertEqual(int(self.reload(self.user).cheque_credit_limit), 50000000)

    def test_approving_twice_is_refused_and_signals_once(self):
        seen = self.listen(cheque_credit_approved)
        request = self.submit()
        self.approve(request)
        self.assertEqual(self.errors(service.approve_request, request, self.admin).status, 409)
        self.assertEqual(seen, [(request.pk, 'approved')])

    def test_a_decided_or_canceled_request_cannot_be_approved(self):
        for finish in (lambda r: service.reject_request(r, self.admin, 'x'), lambda r: service.cancel_request(r, self.user)):
            with self.subTest():
                request = self.submit()
                finish(request)
                self.assertEqual(self.errors(service.approve_request, request, self.admin).status, 409)
                self.assertFalse(self.reload(self.user).can_purchase_with_check)

    def test_the_signal_fires_only_after_commit(self):
        seen = self.listen(cheque_credit_approved)
        request = self.submit()
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            service.approve_request(request, self.admin)
        self.assertEqual(seen, [])
        for callback in callbacks:
            callback()
        self.assertEqual(len(seen), 1)

    def test_a_signal_handler_error_never_breaks_the_approval(self):
        cheque_credit_approved.connect(lambda **kw: 1 / 0, weak=False, dispatch_uid='credit_boom')
        self.addCleanup(cheque_credit_approved.disconnect, dispatch_uid='credit_boom')
        request = self.submit()
        self.approve(request)
        self.assertEqual(self.reload(request).status, 'approved')
        self.assertTrue(self.reload(self.user).can_purchase_with_check)

    def test_the_approval_is_atomic_with_the_permission(self):
        request = self.submit()
        with mock.patch.object(CustomUser.objects.__class__, 'filter', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                service.approve_request(request, self.admin)
        self.assertEqual(self.reload(request).status, 'pending')
        self.assertFalse(self.reload(self.user).can_purchase_with_check)

    def test_an_anonymous_reviewer_is_stored_as_none(self):
        approved = service.approve_request(self.submit(), None)
        self.assertIsNone(approved.reviewed_by)


# ------------------------------------------------------------------ رد
class RejectTests(CreditBase):
    def reject(self, request, reason='مدارک ناقص است', **kwargs):
        with self.captureOnCommitCallbacks(execute=True):
            return service.reject_request(request, self.admin, reason, **kwargs)

    def test_rejection_stores_the_reason_and_leaves_the_permission_off(self):
        request = self.submit()
        rejected = self.reject(request)
        self.assertEqual((rejected.status, rejected.rejection_reason, rejected.reviewed_by), ('rejected', 'مدارک ناقص است', self.admin))
        self.assertIsNotNone(rejected.reviewed_at)
        self.assertFalse(self.reload(self.user).can_purchase_with_check)

    def test_a_reason_is_mandatory(self):
        request = self.submit()
        for empty in ('', '   ', None, '\n\t'):
            with self.subTest(reason=empty):
                self.assertIn('rejection_reason', self.errors(service.reject_request, request, self.admin, empty).errors)
        self.assertEqual(self.reload(request).status, 'pending')

    def test_the_reason_is_normalized_and_length_limited(self):
        request = self.submit()
        self.assertIn('rejection_reason', self.errors(service.reject_request, request, self.admin, 'x' * 301).errors)
        self.assertEqual(self.reject(request, reason='  تصویر \n\x00 ناخوانا ').rejection_reason, 'تصویر ناخوانا')

    def test_rejecting_twice_is_refused_and_signals_once(self):
        seen = self.listen(cheque_credit_rejected)
        request = self.submit()
        self.reject(request)
        self.assertEqual(self.errors(service.reject_request, request, self.admin, 'دوباره').status, 409)
        self.assertEqual(seen, [(request.pk, 'rejected')])
        self.assertEqual(self.reload(request).rejection_reason, 'مدارک ناقص است')

    def test_an_approved_request_cannot_be_rejected_afterwards(self):
        request = self.submit()
        service.approve_request(request, self.admin)
        self.assertEqual(self.errors(service.reject_request, request, self.admin, 'x').status, 409)
        self.assertTrue(self.reload(self.user).can_purchase_with_check)

    def test_the_signal_carries_the_reason_and_fires_after_commit(self):
        reasons = []
        handler = lambda sender, request, **kw: reasons.append(request.rejection_reason)          # noqa: E731
        cheque_credit_rejected.connect(handler, weak=False, dispatch_uid='credit_reason')
        self.addCleanup(cheque_credit_rejected.disconnect, dispatch_uid='credit_reason')
        request = self.submit()
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            service.reject_request(request, self.admin, 'علت')
        self.assertEqual(reasons, [])
        for callback in callbacks:
            callback()
        self.assertEqual(reasons, ['علت'])

    def test_an_internal_note_is_kept_but_limited(self):
        request = self.submit()
        self.assertIn('admin_note', self.errors(service.reject_request, request, self.admin, 'x', admin_note='y' * 501).errors)
        self.assertEqual(self.reject(request, admin_note='تماس گرفته شد').admin_note, 'تماس گرفته شد')


# ------------------------------------------------------------------ پاک‌سازی مدارک
class PurgeTests(CreditBase):
    def decided(self, finish, days_ago):
        user = make_user(f'0912000{600 + ChequeCreditRequest.objects.count():04d}', price_level=2)
        request = self.submit(user=user, files=docs('cheque_book', 'national_card'))
        finish(request, user)
        ChequeCreditRequest.objects.filter(pk=request.pk).update(decided_at=timezone.now() - timedelta(days=days_ago))
        return request

    def approve(self, request, user):
        service.approve_request(request, self.admin)

    def reject(self, request, user):
        service.reject_request(request, self.admin, 'x')

    def cancel(self, request, user):
        service.cancel_request(request, user)

    def purge(self, **kwargs):
        with self.captureOnCommitCallbacks(execute=True):
            return service.purge_expired_documents(**kwargs)

    def test_old_decided_requests_lose_their_images_but_keep_their_data(self):
        old = [self.decided(self.approve, 31), self.decided(self.reject, 40), self.decided(self.cancel, 100)]
        self.assertEqual(len(stored(self.media_root)), 6)
        self.assertEqual(self.purge(), 3)
        self.assertEqual(stored(self.media_root), [])
        for request in old:
            fresh = self.reload(request)
            self.assertEqual(fresh.documents.count(), 0)
            self.assertIsNotNone(fresh.documents_purged_at)
            self.assertEqual(fresh.iban, IBAN)                                    # اطلاعات متنی می‌ماند

    def test_recent_and_pending_requests_are_kept(self):
        recent = self.decided(self.reject, 29)
        pending = self.submit(user=make_user('09120000700', price_level=2))
        ChequeCreditRequest.objects.filter(pk=pending.pk).update(decided_at=timezone.now() - timedelta(days=500))   # داده‌ی ناسازگار
        self.assertEqual(self.purge(), 0)
        self.assertEqual(self.reload(recent).documents.count(), 2)
        self.assertEqual(self.reload(pending).documents.count(), 1)

    def test_a_disabled_setting_purges_nothing(self):
        request = self.decided(self.approve, 400)
        set_retention(0)
        self.assertEqual(self.purge(), 0)
        self.assertEqual(self.reload(request).documents.count(), 2)

    def test_the_configured_days_are_used(self):
        set_retention(7)
        request = self.decided(self.reject, 8)
        keep = self.decided(self.reject, 6)
        self.assertEqual(self.purge(), 1)
        self.assertEqual(self.reload(request).documents.count(), 0)
        self.assertEqual(self.reload(keep).documents.count(), 2)

    def test_it_is_idempotent_and_batched(self):
        for _ in range(3):
            self.decided(self.reject, 60)
        self.assertEqual(self.purge(limit=2), 2)
        self.assertEqual(self.purge(limit=2), 1)
        self.assertEqual(self.purge(), 0)

    def test_one_failing_request_does_not_stop_the_rest(self):
        first, second = self.decided(self.reject, 60), self.decided(self.reject, 59)
        real = ChequeCreditDocument.delete

        def flaky(doc, *args, **kwargs):
            if doc.request_id == first.pk:
                raise RuntimeError('boom')
            return real(doc, *args, **kwargs)
        with mock.patch.object(ChequeCreditDocument, 'delete', flaky), self.assertLogs('accounts.cheque_credit_service', level='ERROR'):
            self.assertEqual(self.purge(), 1)
        self.assertEqual(self.reload(first).documents.count(), 2)
        self.assertIsNone(self.reload(first).documents_purged_at)
        self.assertEqual(self.reload(second).documents.count(), 0)


# ------------------------------------------------------------------ اعتبارسنجی کد ملی (تنظیم ادمین)
class NationalCodeTests(CreditBase):
    BAD_CHECKSUM = '1234567890'                       # ده‌رقمی ولی رقم کنترل نمی‌خواند

    def give(self, code):
        CustomUser.objects.filter(pk=self.user.pk).update(national_code=code)
        return self.reload(self.user)

    def test_the_setting_defaults_to_strict(self):
        self.assertTrue(SiteSettings.load().strict_national_code_validation)

    def test_the_setting_is_in_the_site_settings_admin(self):
        from django.contrib.admin.sites import site
        fields = [f for _, opts in site._registry[SiteSettings].get_fieldsets(None) for f in opts['fields']]
        self.assertIn('strict_national_code_validation', fields)

    def test_the_helper_matches_the_official_algorithm(self):
        for good in (valid_national_code(), valid_national_code()):
            self.assertIsNone(service.national_code_problem(self.give(good)))
        self.assertIn('رقم کنترلی', service.national_code_problem(self.give(self.BAD_CHECKSUM)))

    def test_a_valid_code_is_accepted_when_strict(self):
        self.assertEqual(self.submit().status, 'pending')

    def test_an_invalid_checksum_is_refused_when_strict(self):
        user = self.give(self.BAD_CHECKSUM)
        error = self.errors(service.submit_request, user, form_data(), docs())
        self.assertEqual(set(error.errors), {'national_code'})
        self.assertIn('معتبر نیست', error.errors['national_code'])
        self.assertEqual(error.status, 400)
        self.assertEqual(ChequeCreditRequest.objects.count(), 0)
        self.assertEqual(stored(self.media_root), [])

    def test_an_invalid_checksum_is_accepted_when_strict_is_off(self):
        set_strict(False)
        request = self.submit(user=self.give(self.BAD_CHECKSUM))
        self.assertEqual(request.national_code, self.BAD_CHECKSUM)

    def test_the_toggle_takes_effect_immediately(self):
        user = self.give(self.BAD_CHECKSUM)
        set_strict(False)
        self.assertIsNone(service.national_code_problem(user))
        set_strict(True)
        self.assertIsNotNone(service.national_code_problem(user))

    def test_the_ten_digit_shape_is_required_even_when_strict_is_off(self):
        set_strict(False)
        for bad in ('123', '12345678', 'abcdefghij', '12345abcde'):
            with self.subTest(code=bad):
                user = self.give(bad)
                self.assertIsNotNone(service.national_code_problem(user))
                self.assertIsNotNone(service.eligibility_blocker(user))
                self.assertEqual(self.errors(service.submit_request, user, form_data(), docs()).status, 409)

    def test_all_zero_and_repeated_digit_codes_fail_the_strict_check(self):
        for bad in ('0000000000', '1111111111'):
            with self.subTest(code=bad):
                self.assertIn('معتبر نیست', service.national_code_problem(self.give(bad)))

    def test_the_check_runs_again_under_the_lock(self):
        user = self.give(self.BAD_CHECKSUM)
        with mock.patch('accounts.cheque_credit_service.national_code_problem', side_effect=[None, 'خطا']):
            error = self.errors(service.submit_request, user, form_data(), docs())
        self.assertEqual(error.errors, {'national_code': 'خطا'})
        self.assertEqual(ChequeCreditRequest.objects.count(), 0)
