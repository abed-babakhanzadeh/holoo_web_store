"""
تست‌های اکشن‌های ادمین چرخه‌ی تأیید تجاری (approve_selected/reject_selected) + قفل readonly
بودن approval_status در فرم تغییر — همان الگوی promotions.admin.CouponAdmin.bulk_generate
(اکشن با صفحه‌ی میانی) که در accounts/admin.py تکرار شده است.
"""

from django.contrib.admin import helpers
from django.test import TestCase
from django.urls import reverse

from accounts.models import ApprovalStatus, CustomUser


def _complete_profile_user(**extra):
    fields = dict(first_name='سارا', last_name='محمدی', national_code='9876543210')
    fields.update(extra)
    return CustomUser.objects.create_user(phone_number=fields.pop('phone_number', '09130001001'), **fields)


class ApprovalAdminActionTests(TestCase):
    def setUp(self):
        self.superuser = CustomUser.objects.create_superuser(phone_number='09130009001', password='StrongPass123!')
        self.client.force_login(self.superuser)
        self.changelist_url = reverse('admin:accounts_customuser_changelist')

    def _action_post(self, action, user_ids, **extra):
        data = {'action': action, helpers.ACTION_CHECKBOX_NAME: [str(pk) for pk in user_ids], **extra}
        return self.client.post(self.changelist_url, data, follow=False)

    # ----- approve_selected -----

    def test_approve_requires_exactly_one_selected_row(self):
        u1 = _complete_profile_user(phone_number='09130001002')
        u2 = _complete_profile_user(phone_number='09130001003')
        response = self._action_post('approve_selected', [u1.pk, u2.pk])
        self.assertEqual(response.status_code, 302)  # ریدایرکت به همان changelist با پیام خطا
        u1.refresh_from_db(); u2.refresh_from_db()
        self.assertEqual(u1.approval_status, ApprovalStatus.PENDING)
        self.assertEqual(u2.approval_status, ApprovalStatus.PENDING)

    def test_approve_shows_intermediate_form_for_single_selection(self):
        user = _complete_profile_user(phone_number='09130001004')
        response = self._action_post('approve_selected', [user.pk])
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'تأیید و تعیین سطح قیمت')

    def test_approve_blocks_incomplete_profile_user_before_showing_form(self):
        incomplete = CustomUser.objects.create_user(phone_number='09130001005')
        response = self._action_post('approve_selected', [incomplete.pk])
        self.assertEqual(response.status_code, 302)
        incomplete.refresh_from_db()
        self.assertEqual(incomplete.approval_status, ApprovalStatus.PENDING)

    def test_approve_apply_sets_approved_and_price_level(self):
        user = _complete_profile_user(phone_number='09130001006')
        response = self._action_post('approve_selected', [user.pk], apply='1', price_level='6')
        self.assertEqual(response.status_code, 302)
        user.refresh_from_db()
        self.assertEqual(user.approval_status, ApprovalStatus.APPROVED)
        self.assertEqual(user.price_level, 6)
        self.assertEqual(user.approved_by_id, self.superuser.pk)

    def test_approve_apply_with_out_of_range_price_level_is_rejected_by_form(self):
        user = _complete_profile_user(phone_number='09130001007')
        response = self._action_post('approve_selected', [user.pk], apply='1', price_level='11')
        self.assertEqual(response.status_code, 200)  # فرم دوباره با خطا رندر می‌شود، نه ریدایرکت موفق
        user.refresh_from_db()
        self.assertEqual(user.approval_status, ApprovalStatus.PENDING)

    def test_approve_apply_on_already_approved_user_shows_warning_and_is_noop(self):
        user = _complete_profile_user(phone_number='09130001008')
        user.approve(price_level=2)
        response = self._action_post('approve_selected', [user.pk], apply='1', price_level='9')
        self.assertEqual(response.status_code, 302)
        user.refresh_from_db()
        self.assertEqual(user.price_level, 2)  # دست‌نخورده

    # ----- reject_selected -----

    def test_reject_apply_sets_rejected_with_reason(self):
        user = _complete_profile_user(phone_number='09130001009')
        response = self._action_post('reject_selected', [user.pk], apply='1', reason='مدارک ناقص')
        self.assertEqual(response.status_code, 302)
        user.refresh_from_db()
        self.assertEqual(user.approval_status, ApprovalStatus.REJECTED)
        self.assertEqual(user.rejection_reason, 'مدارک ناقص')
        self.assertEqual(user.rejected_by_id, self.superuser.pk)

    def test_reject_requires_exactly_one_selected_row(self):
        u1 = _complete_profile_user(phone_number='09130001010')
        u2 = _complete_profile_user(phone_number='09130001011')
        response = self._action_post('reject_selected', [u1.pk, u2.pk])
        self.assertEqual(response.status_code, 302)
        u1.refresh_from_db(); u2.refresh_from_db()
        self.assertEqual(u1.approval_status, ApprovalStatus.PENDING)
        self.assertEqual(u2.approval_status, ApprovalStatus.PENDING)

    # ----- readonly در فرم تغییر عادی -----

    def test_approval_status_cannot_be_changed_via_normal_change_form(self):
        """
        نکته‌ی امنیتی کلیدی: approval_status در change_form جنگو readonly است، پس حتی اگر
        کسی مقدار را در POST دستکاری کند، جنگو آن را نادیده می‌گیرد (فیلدهای readonly اصلاً
        جزو ModelForm نیستند).
        """
        user = _complete_profile_user(phone_number='09130001012')
        change_url = reverse('admin:accounts_customuser_change', args=[user.pk])
        get_response = self.client.get(change_url)
        self.assertEqual(get_response.status_code, 200)

        post_data = {
            'phone_number': user.phone_number, 'status': user.status, 'price_level': '5',
            'first_name': user.first_name, 'last_name': user.last_name, 'national_code': user.national_code,
            'approval_status': ApprovalStatus.APPROVED,  # تلاش برای دستکاری دستی
            'is_active': 'on', 'date_joined_0': '2024-01-01', 'date_joined_1': '00:00:00',
            'address_set-TOTAL_FORMS': '0', 'address_set-INITIAL_FORMS': '0',
            'address_set-MIN_NUM_FORMS': '0', 'address_set-MAX_NUM_FORMS': '1000',
        }
        self.client.post(change_url, post_data)
        user.refresh_from_db()
        self.assertEqual(user.approval_status, ApprovalStatus.PENDING)  # دستکاری بی‌اثر بود
