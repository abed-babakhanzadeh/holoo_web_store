"""
ورود گروهی مشتریان هلو به سایت (holoo/customers.py)، تأیید خودکار مشتری قبلی هلو هنگام تکمیل پروفایل، و سیاستِ
«فقط جاهای خالی» برای بازنویسی مشتری در هلو. هیچ‌کدام به شبکه نمی‌روند.
"""
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from accounts.models import ApprovalStatus, CustomUser, UserStatus
from holoo.customers import apply_plan, build_plan, clean_name, price_level_of, split_name
from holoo.tasks import fill_blank_update, sync_customer


def row(code, name, mobile, erp=None, price=1, **extra):
    data = {'Code': str(code), 'Name': name, 'Mobile': mobile, 'ErpCode': erp or f'ERP{code}=', 'IsPurchaser': True, 'IsActive': True,
            'IsBlackList': False, 'selectedPriceType': price, 'BedSarfasl': f'10300{code}'}
    data.update(extra)
    return data


class NameAndPriceTests(SimpleTestCase):
    def test_clean_name_turns_arabic_letters_into_persian_and_squeezes_spaces(self):
        self.assertEqual(clean_name('  ارمکان   دني وان '), 'ارمکان دنی وان')
        self.assertEqual(clean_name('كاظمي'), 'کاظمی')

    def test_split_name_uses_last_word_as_family(self):
        self.assertEqual(split_name('مهدی اشکوه'), ('مهدی', 'اشکوه'))
        self.assertEqual(split_name('حاج عباس اشعری'), ('حاج عباس', 'اشعری'))
        self.assertEqual(split_name('انديشمند'), ('اندیشمند', ''))
        self.assertEqual(split_name(None), ('', ''))

    def test_price_level_comes_from_holoo_and_defaults_to_one(self):
        self.assertEqual(price_level_of({'selectedPriceType': 3}), 3)
        self.assertEqual(price_level_of({'selectedPriceType': 0}), 1)
        self.assertEqual(price_level_of({'selectedPriceType': 99}), 1)
        self.assertEqual(price_level_of({}), 1)
        self.assertEqual(price_level_of({'selectedPriceType': 10}), 10)


class BuildPlanTests(SimpleTestCase):
    def reasons(self, plan):
        return {r['Code']: reason for r, reason, _ in plan.skipped}

    def test_filters_and_reasons(self):
        rows = [
            row(1, 'خوب', '09121111111'),
            row(2, 'تأمین‌کننده', '09122222222', IsPurchaser=False),
            row(3, 'غیرفعال', '09123333333', IsActive=False),
            row(4, 'سیاه', '09124444444', IsBlackList=True),
            row(5, 'بی‌موبایل', ''),
            row(6, 'موبایل بد', '02155814834'),
            row(7, 'ساخته‌ی سایت', '09127777777', WebId='55001'),
            row(8, 'webid خالی', '09128888888', WebId='null'),
            {**row(9, 'بی‌شناسه', '09129999999'), 'ErpCode': ''},
        ]
        plan = build_plan(rows)
        self.assertEqual([m for m, _ in plan.candidates], ['09121111111', '09128888888'])
        self.assertEqual(self.reasons(plan), {'2': 'not_purchaser', '3': 'inactive', '4': 'blacklisted', '5': 'no_mobile',
                                              '6': 'invalid_mobile', '7': 'site_origin', '9': 'no_erp'})

    def test_shared_mobile_picks_the_smallest_code_and_reports_the_rest(self):
        plan = build_plan([row(1039, 'آقایی شیشه', '09302512058'), row(3393, 'آقایی', '09302512058'), row(500, 'اول', '+989302512058')])
        self.assertEqual([r['Code'] for _, r in plan.candidates], ['500'])
        self.assertEqual(sorted(self.reasons(plan)), ['1039', '3393'])
        self.assertEqual(set(self.reasons(plan).values()), {'shared_mobile'})

    def test_mobile_formats_are_normalized(self):
        plan = build_plan([row(1, 'a', '9121234567'), row(2, 'b', '+98 912 123 4568')])
        self.assertEqual([m for m, _ in plan.candidates], ['09121234567', '09121234568'])


class ApplyPlanTests(TestCase):
    def apply(self, rows, **kw):
        return apply_plan(build_plan(rows), **kw)

    def test_creates_users_with_holoo_code_price_and_a_pending_profile(self):
        report = self.apply([row(10, 'مهدی اشکوه', '09121111111', price=2)])
        self.assertEqual(report.counts['created'], 1)
        user = CustomUser.objects.get(phone_number='09121111111')
        self.assertEqual((user.erp_code, user.price_level, user.status, user.approval_status),
                         ('ERP10=', 2, UserStatus.PENDING_PROFILE, ApprovalStatus.PENDING))
        self.assertEqual((user.first_name, user.last_name, user.national_code), ('مهدی', 'اشکوه', None))
        self.assertEqual((user.holoo_customer_code, user.holoo_bed_sarfasl, user.holoo_full_name), ('10', '1030010', 'مهدی اشکوه'))
        self.assertTrue(user.imported_from_holoo)
        self.assertFalse(user.has_usable_password())     # ورود فقط با کد یکبارمصرف

    def test_dry_run_writes_nothing(self):
        report = self.apply([row(10, 'مهدی اشکوه', '09121111111')], apply=False)
        self.assertEqual(report.counts['would_create'], 1)
        self.assertFalse(CustomUser.objects.filter(phone_number='09121111111').exists())

    def test_running_twice_is_safe(self):
        rows = [row(10, 'مهدی اشکوه', '09121111111')]
        self.apply(rows)
        report = self.apply(rows)
        self.assertEqual((report.counts['created'], report.counts['already']), (0, 1))
        self.assertEqual(CustomUser.objects.filter(phone_number='09121111111').count(), 1)

    def test_an_existing_pending_user_with_a_complete_profile_is_linked_and_approved_with_the_holoo_price(self):
        user = CustomUser.objects.create_user('09121111111', first_name='الف', last_name='ب', national_code='0012345678')
        report = self.apply([row(10, 'نام هلو', '09121111111', price=3)])
        self.assertEqual(report.counts['approved_existing'], 1)
        user.refresh_from_db()
        self.assertEqual((user.erp_code, user.approval_status, user.price_level), ('ERP10=', ApprovalStatus.APPROVED, 3))
        self.assertEqual((user.first_name, user.last_name), ('الف', 'ب'))       # نام کاربر بازنویسی نمی‌شود

    def test_an_existing_user_without_a_complete_profile_is_linked_but_stays_pending(self):
        user = CustomUser.objects.create_user('09121111111')
        report = self.apply([row(10, 'نام هلو', '09121111111', price=2)])
        self.assertEqual(report.counts['linked'], 1)
        user.refresh_from_db()
        self.assertEqual((user.erp_code, user.approval_status, user.price_level, user.imported_from_holoo),
                         ('ERP10=', ApprovalStatus.PENDING, 2, True))

    def test_an_approved_users_price_is_never_changed_by_the_import(self):
        user = CustomUser.objects.create_user('09121111111', first_name='الف', last_name='ب', national_code='0012345678')
        user.approve(price_level=5)
        self.apply([row(10, 'نام هلو', '09121111111', price=1)])
        user.refresh_from_db()
        self.assertEqual((user.price_level, user.erp_code), (5, 'ERP10='))

    def test_a_rejected_user_is_linked_but_not_approved(self):
        user = CustomUser.objects.create_user('09121111111', first_name='الف', last_name='ب', national_code='0012345678')
        user.reject('نه')
        self.apply([row(10, 'نام هلو', '09121111111')])
        user.refresh_from_db()
        self.assertEqual((user.approval_status, user.erp_code), (ApprovalStatus.REJECTED, 'ERP10='))

    def test_conflicts_are_reported_not_overwritten(self):
        CustomUser.objects.create_user('09121111111', erp_code='OTHER=')
        CustomUser.objects.create_user('09122222222', erp_code='ERP11=')
        report = self.apply([row(10, 'الف', '09121111111'), row(11, 'ب', '09123333333', erp='ERP11=')])
        self.assertEqual(report.counts['conflict'], 2)
        self.assertEqual(CustomUser.objects.get(phone_number='09121111111').erp_code, 'OTHER=')
        self.assertFalse(CustomUser.objects.filter(phone_number='09123333333').exists())


class AutoApprovalOnProfileCompletionTests(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(
            '09121110099', first_name='مهدی', last_name='اشکوه', erp_code='ERP10=', price_level=3, status=UserStatus.PENDING_PROFILE,
            imported_from_holoo=True)
        self.client.force_login(self.user)

    def complete(self):
        return self.client.post(reverse('accounts:profile_complete'), {
            'first_name': 'مهدی', 'last_name': 'اشکوه', 'national_code': '1234567890',
            'password': 'StrongPass123!', 'confirm_password': 'StrongPass123!'})

    def test_the_prefilled_name_is_shown_on_first_visit(self):
        response = self.client.get(reverse('accounts:profile_complete'))
        self.assertContains(response, 'value="مهدی"')
        self.assertContains(response, 'value="اشکوه"')

    def test_completing_the_profile_approves_a_holoo_customer_at_the_holoo_price(self):
        with mock.patch('holoo.receivers.sync_user_to_holoo.delay'):
            self.complete()
        self.user.refresh_from_db()
        self.assertEqual((self.user.approval_status, self.user.price_level), (ApprovalStatus.APPROVED, 3))
        self.assertTrue(self.user.can_order())

    def test_a_regular_new_user_still_waits_for_the_admin(self):
        CustomUser.objects.filter(pk=self.user.pk).update(imported_from_holoo=False)
        with mock.patch('holoo.receivers.sync_user_to_holoo.delay'):
            self.complete()
        self.user.refresh_from_db()
        self.assertEqual(self.user.approval_status, ApprovalStatus.PENDING)

    def test_completing_via_the_profile_page_also_approves(self):
        with mock.patch('holoo.receivers.sync_user_to_holoo.delay'):
            self.client.post(reverse('accounts:profile'), {'first_name': 'مهدی', 'last_name': 'اشکوه', 'national_code': '1234567890', 'email': ''})
        self.user.refresh_from_db()
        self.assertEqual((self.user.approval_status, self.user.price_level), (ApprovalStatus.APPROVED, 3))

    def test_an_identity_change_after_approval_still_needs_the_admin(self):
        with mock.patch('holoo.receivers.sync_user_to_holoo.delay'):
            self.complete()
            self.client.post(reverse('accounts:profile'), {'first_name': 'نام‌تازه', 'last_name': 'اشکوه', 'national_code': '1234567890', 'email': ''})
        self.user.refresh_from_db()
        self.assertEqual(self.user.approval_status, ApprovalStatus.PENDING)

    def test_a_rejected_customer_is_never_auto_approved(self):
        CustomUser.objects.filter(pk=self.user.pk).update(approval_status=ApprovalStatus.REJECTED)
        with mock.patch('holoo.receivers.sync_user_to_holoo.delay'):
            self.complete()
        self.user.refresh_from_db()
        self.assertEqual(self.user.approval_status, ApprovalStatus.REJECTED)


class FillBlankOnlyTests(SimpleTestCase):
    UPDATE = {'first_name': 'علی', 'last_name': 'رضایی', 'address': 'تهران، خیابان', 'phone_number': '09121111111',
              'national_code': '0012345678', 'province': 'تهران', 'city': 'تهران', 'postal_code': '1234567890'}

    def test_nothing_is_written_over_existing_holoo_data(self):
        full = {'NationalId': '1', 'Mobile': '09121111111', 'Address': 'قدیمی', 'City': 'قم', 'Ostan': 'قم', 'ZipCode': '1'}
        self.assertEqual(fill_blank_update(full, dict(self.UPDATE), client_id='5'), {})

    def test_only_blank_fields_are_filled_and_the_name_never(self):
        sparse = {'Mobile': '09121111111', 'Address': 'قدیمی'}
        out = fill_blank_update(sparse, dict(self.UPDATE), client_id='5')
        self.assertEqual(out, {'national_code': '0012345678'})
        self.assertNotIn('first_name', out)

    def test_an_address_fills_together_with_its_blank_parts(self):
        out = fill_blank_update({'NationalId': '1', 'Mobile': 'x', 'City': 'قم'}, dict(self.UPDATE), client_id='5')
        self.assertEqual(out, {'address': 'تهران، خیابان', 'province': 'تهران', 'postal_code': '1234567890'})

    def test_a_customer_with_another_webid_is_left_alone(self):
        self.assertEqual(fill_blank_update({'WebId': '777'}, dict(self.UPDATE), client_id='5'), {})
        self.assertEqual(fill_blank_update({'WebId': '5'}, dict(self.UPDATE), client_id='5'), {'national_code': '0012345678', 'phone_number': '09121111111', 'address': 'تهران، خیابان', 'province': 'تهران', 'city': 'تهران', 'postal_code': '1234567890'})


class SyncCustomerPolicyTests(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user('09121110055', first_name='علی', last_name='رضایی', national_code='0012345678',
                                                   erp_code='ERP10=', imported_from_holoo=True)

    def client_with_row(self, row_data):
        client = mock.Mock()
        client.config.write_is_mock = False
        client._lookup_customer.return_value = row_data
        client._client_id.side_effect = lambda raw: str(raw)
        client.update_person.return_value = {'success': True}
        return client

    def test_an_imported_customer_only_gets_blank_fields_updated(self):
        client = self.client_with_row({'Mobile': '09121110055', 'Address': 'قدیمی'})
        self.assertTrue(sync_customer(self.user, client)['success'])
        kwargs = client.update_person.call_args.kwargs
        self.assertEqual(kwargs['national_code'], '0012345678')
        self.assertEqual(kwargs['web_id'], self.user.id)
        for forbidden in ('first_name', 'last_name', 'address'):
            self.assertNotIn(forbidden, kwargs)

    def test_nothing_to_fill_means_no_call_to_holoo_but_the_user_becomes_active(self):
        client = self.client_with_row({'NationalId': '1', 'Mobile': 'x', 'Address': 'y'})
        self.assertTrue(sync_customer(self.user, client)['success'])
        client.update_person.assert_not_called()
        self.user.refresh_from_db()
        self.assertEqual(self.user.status, UserStatus.ACTIVE)

    def test_an_unreadable_holoo_row_is_a_transient_failure_not_an_overwrite(self):
        client = self.client_with_row(None)
        result = sync_customer(self.user, client)
        self.assertFalse(result['success'])
        self.assertTrue(result['transient'])
        client.update_person.assert_not_called()

    def test_a_regular_site_customer_is_still_fully_updated(self):
        CustomUser.objects.filter(pk=self.user.pk).update(imported_from_holoo=False)
        self.user.refresh_from_db()
        client = self.client_with_row({})
        sync_customer(self.user, client)
        self.assertEqual(client.update_person.call_args.kwargs['first_name'], 'علی')
        client._lookup_customer.assert_not_called()


class ImportCommandAndAdminTests(TestCase):
    ROWS = [row(10, 'مهدی اشکوه', '09121111111', price=2), row(11, 'کاظمی', '09122222222')]

    def run_cmd(self, *args):
        out = StringIO()
        with mock.patch('holoo.management.commands.import_holoo_customers.fetch_customer_rows', return_value=self.ROWS):
            call_command('import_holoo_customers', *args, stdout=out)
        return out.getvalue()

    def test_command_is_a_dry_run_by_default(self):
        text = self.run_cmd()
        self.assertIn('--apply', text)
        self.assertEqual(CustomUser.objects.filter(imported_from_holoo=True).count(), 0)

    def test_command_apply_creates_users(self):
        self.run_cmd('--apply')
        self.assertEqual(CustomUser.objects.filter(imported_from_holoo=True).count(), 2)

    def test_command_fails_clearly_when_holoo_cannot_be_read(self):
        from django.core.management import CommandError
        with mock.patch('holoo.management.commands.import_holoo_customers.fetch_customer_rows', return_value=None):
            with self.assertRaises(CommandError):
                call_command('import_holoo_customers', stdout=StringIO())

    def test_admin_preview_then_apply(self):
        admin = CustomUser.objects.create_superuser('09120000011', password='x')
        self.client.force_login(admin)
        url = reverse('admin:accounts_customuser_import_holoo')
        with mock.patch('holoo.customers.fetch_customer_rows', return_value=self.ROWS), \
                mock.patch('holoo.client.HolooClient'):
            preview = self.client.get(url)
            self.assertContains(preview, 'پیش‌نمایش')
            self.assertEqual(CustomUser.objects.filter(imported_from_holoo=True).count(), 0)
            csv_response = self.client.get(url + '?csv=1')
            self.assertEqual(csv_response['Content-Type'].split(';')[0], 'text/csv')
            self.client.post(url)
        self.assertEqual(CustomUser.objects.filter(imported_from_holoo=True).count(), 2)

    def test_admin_import_needs_the_add_permission(self):
        staff = CustomUser.objects.create_user('09120000012', is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(reverse('admin:accounts_customuser_import_holoo')).status_code, 403)
