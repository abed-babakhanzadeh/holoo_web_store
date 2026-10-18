"""
پیامک «کد تخفیف اختصاصی برای شما صادر شد» (promotions/assigned_notice.py + CouponAdmin.save_formset).
هیچ پیامک واقعی ارسال نمی‌شود؛ ردیف Notification بررسی می‌شود.
"""
from datetime import timedelta
from unittest import mock

from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomUser
from accounts.testing import make_approved_user
from notifications.models import Notification, NotificationSetting

from .models import Coupon, UserCoupon
from .testing import make_coupon
from .tests_coupon_admin import AdminBase, jalali_fields

KEY = 'coupon_assigned_customer'


class AssignedCouponSmsTests(AdminBase):
    def setUp(self):
        super().setUp()
        self.customer = make_approved_user('09125551111')
        self.customer.first_name, self.customer.last_name = 'سارا', 'احمدی'
        self.customer.save(update_fields=['first_name', 'last_name'])
        patcher = mock.patch('notifications.tasks.deliver_notification.delay')
        patcher.start()
        self.addCleanup(patcher.stop)

    def post_change(self, coupon, users, **extra):
        data = {
            'code': coupon.code, 'title': coupon.title, 'description': '', 'is_active': 'on' if coupon.is_active else '',
            'kind': coupon.kind, 'value': str(coupon.value), 'max_discount_amount': '', 'scope': coupon.scope,
            'min_cart_amount': str(coupon.min_cart_amount), 'allow_with_promotions': '', 'total_limit': '', 'per_user_limit': '1',
            'first_order_only': '', 'audience': 'assigned', 'min_loyalty_level': '0', 'is_claimable': '', 'claim_limit': '', 'terms': '',
            'assignments-TOTAL_FORMS': str(len(users)), 'assignments-INITIAL_FORMS': '0', 'assignments-MIN_NUM_FORMS': '0',
            'assignments-MAX_NUM_FORMS': '1000',
            'redemptions-TOTAL_FORMS': '0', 'redemptions-INITIAL_FORMS': '0', 'redemptions-MIN_NUM_FORMS': '0', 'redemptions-MAX_NUM_FORMS': '0',
        }
        data['products'] = [str(pk) for pk in coupon.products.values_list('pk', flat=True)]
        data['categories'] = [str(pk) for pk in coupon.categories.values_list('pk', flat=True)]
        if coupon.ends_at:
            data.update(jalali_fields('ends_at', coupon.ends_at))
        for i, user in enumerate(users):
            data[f'assignments-{i}-user'] = str(user.pk)
            data[f'assignments-{i}-source'] = 'admin'
        data.update(extra)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse('admin:promotions_coupon_change', args=[coupon.pk]), data, follow=True)
        context = response.context
        if context is not None and 'adminform' in context:                       # فرم رد شده: علت را همین‌جا نشان بده
            self.fail(f'{context["adminform"].form.errors} {[f.errors for f in context["inline_admin_formsets"][0].formset.forms]}')
        return response

    def sms(self):
        return Notification.objects.filter(template_key=KEY)

    def make(self, **fields):
        fields.setdefault('audience', Coupon.AUDIENCE_ASSIGNED)
        return make_coupon(code='SARA20', title='جشنواره سارا', **fields)

    def test_assigning_a_code_in_the_admin_sends_one_sms_with_code_scope_expiry_and_panel_link(self):
        coupon = self.make(value=20, products=[self.product], ends_at=timezone.now() + timedelta(days=10))
        response = self.post_change(coupon, [self.customer])
        self.assertContains(response, 'پیامک اطلاع‌رسانی کد برای 1 کاربر در صف ارسال قرار گرفت')
        notice = self.sms().get()
        self.assertEqual(notice.recipient, '09125551111')
        for wanted in ('سارا احمدی گرامی', '«جشنواره سارا»', '20٪', 'فقط برای کالای ادمین', 'اعتبار تا', 'کد: SARA20',
                       'http://testserver' + reverse('promotions:my_codes')):
            self.assertIn(wanted, notice.text)
        self.assertTrue(UserCoupon.objects.filter(coupon=coupon, user=self.customer).exists())

    def test_cart_wide_fixed_and_free_shipping_codes_read_naturally(self):
        fixed = self.make(kind=Coupon.KIND_FIXED, value=50000, min_cart_amount=300000)
        self.post_change(fixed, [self.customer])
        text = self.sms().get().text
        self.assertIn('50,000 تومان', text)
        self.assertIn('روی کل سبد', text)
        self.assertIn('حداقل سبد 300,000 تومان', text)

        Notification.objects.all().delete()
        free = make_coupon(code='SHIP1', title='ارسال رایگان ویژه', kind=Coupon.KIND_FREE_SHIPPING, audience=Coupon.AUDIENCE_ASSIGNED)
        self.post_change(free, [self.customer])
        self.assertIn('ارسال رایگان با پیک درون‌شهری', self.sms().get().text)

    def test_user_without_a_name_is_greeted_generically(self):
        nameless = CustomUser.objects.create_user(phone_number='09125552222')               # هنوز پروفایل را کامل نکرده
        coupon = self.make(value=10)
        self.post_change(coupon, [nameless])
        self.assertTrue(self.sms().get().text.startswith('کاربر گرامی'))

    def test_only_newly_added_users_are_notified_not_existing_assignments(self):
        coupon = self.make(value=10)
        UserCoupon.objects.create(coupon=coupon, user=self.customer)
        other = make_approved_user('09125553333')
        data_users = [other]
        self.post_change(coupon, data_users)
        self.assertEqual([n.recipient for n in self.sms()], ['09125553333'])

    def test_many_users_each_get_their_own_sms(self):
        coupon = self.make(value=10)
        users = [make_approved_user(f'0912555{4000 + i}') for i in range(3)]
        self.post_change(coupon, users)
        self.assertEqual(sorted(n.recipient for n in self.sms()), sorted(u.phone_number for u in users))

    def test_inactive_or_expired_codes_do_not_text_but_tell_the_admin_why(self):
        for fields in ({'active': False}, {'expired': True}):
            with self.subTest(fields=fields):
                UserCoupon.objects.all().delete()
                Coupon.objects.all().delete()
                coupon = self.make(**fields)
                response = self.post_change(coupon, [self.customer])
                self.assertEqual(self.sms().count(), 0)
                self.assertContains(response, 'پیامکی نرفت')
                self.assertTrue(UserCoupon.objects.filter(coupon=coupon, user=self.customer).exists())     # تخصیص ثبت شده است

    def test_turning_the_template_off_in_the_notification_panel_stops_the_sms_with_a_warning(self):
        from notifications.models import sync_notification_settings
        sync_notification_settings()
        NotificationSetting.objects.filter(template_key=KEY).update(is_enabled=False)
        response = self.post_change(self.make(value=10), [self.customer])
        self.assertEqual(self.sms().count(), 0)
        self.assertContains(response, 'خاموش است')

    def test_saving_the_coupon_again_without_new_assignments_sends_nothing(self):
        coupon = self.make(value=10)
        self.post_change(coupon, [self.customer])
        self.assertEqual(self.sms().count(), 1)
        UserCoupon.objects.filter(coupon=coupon).delete()
        UserCoupon.objects.create(coupon=coupon, user=self.customer)
        data = {'assignments-TOTAL_FORMS': '1', 'assignments-INITIAL_FORMS': '1',
                'assignments-0-id': str(UserCoupon.objects.get().pk), 'assignments-0-coupon': str(coupon.pk),
                'assignments-0-user': str(self.customer.pk), 'assignments-0-source': 'admin'}
        self.post_change(coupon, [], **data)
        self.assertEqual(self.sms().count(), 1)

    def test_self_claimed_and_loyalty_codes_get_no_extra_sms(self):
        coupon = self.make(value=10)
        UserCoupon.objects.create(coupon=coupon, user=self.customer, source=UserCoupon.SOURCE_CLAIMED)
        UserCoupon.objects.create(coupon=coupon, user=make_approved_user('09125555555'), source=UserCoupon.SOURCE_AUTO)
        self.assertEqual(self.sms().count(), 0)

    def test_template_is_registered_in_the_notification_registry(self):
        from notifications.templates_registry import TEMPLATES
        self.assertIn(KEY, TEMPLATES)
