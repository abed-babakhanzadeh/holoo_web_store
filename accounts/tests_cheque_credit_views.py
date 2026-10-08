"""
فاز F2 درخواست خرید چکی: صفحه‌ی فرم/وضعیت در پنل کاربری، ثبت و انصراف، نمایش امن مدارک مشتری، حفظ سبد خرید، جلوگیری از فرم تکراری،
منوی پنل و اتصال به صفحه‌ی تسویه‌حساب. MEDIA_ROOT هر تست موقت است؛ به media/ واقعی چیزی نوشته نمی‌شود.
"""
import io
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from accounts import cheque_credit_service as service
from accounts.cheque_credit import ChequeCreditDocument, ChequeCreditRequest
from accounts.models import CustomUser
from accounts.tests_cheque_credit import IBAN, form_data, make_user, set_strict
from cart.models import Cart, CartItem
from orders.tests import CheckoutTestBase
from orders.tests_cheques import MediaIsolation, make_image, upload
from orders.tests_payment_options import set_policy
from products.pricing import VIP_CHEQUE_DISABLED, VIP_CHEQUE_REQUEST


def post_data(**overrides):
    data = form_data(**overrides)
    data['doc_cheque_book'] = [upload(make_image(), 'book.jpg')]
    return data


class ViewBase(MediaIsolation, TestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user('09120000801', price_level=2, first_name='علی', last_name='رضایی')
        self.admin = make_user('09120000802', is_staff=True, is_superuser=True)
        self.client.force_login(self.user)
        self.url = reverse('accounts:cheque_credit')

    def reload(self, obj):
        return type(obj).objects.get(pk=obj.pk)

    def submit_via_service(self, user=None, **overrides):
        with self.captureOnCommitCallbacks(execute=True):
            return service.submit_request(user or self.user, form_data(**overrides), [('cheque_book', upload(make_image()))])


class AccessAndStateTests(ViewBase):
    def test_login_is_required_everywhere(self):
        request = self.submit_via_service()
        document = request.documents.get()
        anonymous = Client()
        for url in (self.url, reverse('accounts:cheque_credit_cancel', args=[request.public_id]),
                    reverse('accounts:cheque_credit_document', args=[document.public_id])):
            with self.subTest(url=url):
                self.assertEqual(anonymous.get(url).status_code, 302)
                self.assertIn('login', anonymous.get(url)['Location'])

    def test_the_old_placeholder_url_redirects_to_the_real_page(self):
        response = self.client.get(reverse('accounts:soon_check_request'))
        self.assertRedirects(response, self.url, fetch_redirect_response=False)

    def test_an_eligible_customer_sees_the_form_with_read_only_identity(self):
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        for name in ('business_name', 'bank_name', 'account_holder', 'iban', 'requested_limit', 'monthly_turnover', 'description',
                     'doc_cheque_book', 'doc_national_card', 'doc_business_license', 'doc_other'):
            self.assertContains(page, f'name="{name}"')
        self.assertContains(page, 'id="credit-form"')
        self.assertContains(page, 'accept="image/jpeg,image/png,image/webp"')
        self.assertContains(page, self.user.national_code)
        self.assertContains(page, 'قیمت چکی')                                   # افشای اثر قیمتی
        self.assertNotContains(page, 'name="national_code"')                    # هویت از پروفایل است، نه ورودی
        self.assertNotContains(page, 'name="first_name"')
        self.assertNotContains(page, 'application/pdf')

    def test_the_form_is_prefilled_from_the_profile(self):
        CustomUser.objects.filter(pk=self.user.pk).update(business_name='فروشگاه من')
        page = self.client.get(self.url)
        self.assertContains(page, 'value="فروشگاه من"')
        self.assertContains(page, 'value="علی رضایی"')

    def test_a_cheque_customer_gets_an_explanation_and_no_form(self):
        cheque_customer = make_user('09120000803', price_level=1)
        client = Client()
        client.force_login(cheque_customer)
        page = client.get(self.url)
        self.assertContains(page, 'data-testid="credit-blocked"')
        self.assertNotContains(page, 'id="credit-form"')

    def test_a_permitted_customer_sees_the_green_box_and_no_form(self):
        CustomUser.objects.filter(pk=self.user.pk).update(can_purchase_with_check=True)
        page = self.client.get(self.url)
        self.assertContains(page, 'data-testid="credit-permitted"')
        self.assertContains(page, reverse('orders:checkout'))
        self.assertNotContains(page, 'id="credit-form"')

    def test_an_invalid_national_code_blocks_the_form_with_a_profile_link(self):
        CustomUser.objects.filter(pk=self.user.pk).update(national_code='1234567890')
        page = self.client.get(self.url)
        self.assertContains(page, 'data-testid="credit-national-code"')
        self.assertContains(page, reverse('accounts:profile'))
        self.assertNotContains(page, 'id="credit-form"')
        set_strict(False)
        self.assertContains(self.client.get(self.url), 'id="credit-form"')

    def test_a_vip_sees_the_form_only_under_the_request_policy(self):
        vip = make_user('09120000804', price_level=3)
        client = Client()
        client.force_login(vip)
        set_policy(VIP_CHEQUE_DISABLED)
        self.assertNotContains(client.get(self.url), 'id="credit-form"')
        set_policy(VIP_CHEQUE_REQUEST)
        self.assertContains(client.get(self.url), 'id="credit-form"')


class SubmitViewTests(ViewBase):
    def test_a_valid_post_creates_the_request_and_redirects_to_the_status(self):
        response = self.client.post(self.url, post_data())
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        request = ChequeCreditRequest.objects.get()
        self.assertEqual((request.user, request.status, request.iban), (self.user, 'pending', IBAN))
        self.assertEqual(request.documents.count(), 1)
        page = self.client.get(self.url)
        self.assertContains(page, 'در انتظار بررسی')
        self.assertContains(page, 'سبد خرید شما حفظ شده است')

    def test_the_cart_is_untouched(self):
        cart = Cart.objects.create(user=self.user)
        from products.models import Category, Product
        product = Product.objects.create(name='کالا', slug='credit-cart-p', erp_code='ERP-CC-1', price=1000, stock=5,
                                         category=Category.objects.create(name='c', slug='credit-cart-c'))
        CartItem.objects.create(cart=cart, product=product, quantity=3)
        self.client.post(self.url, post_data())
        self.assertEqual(Cart.objects.get(user=self.user).items.get().quantity, 3)

    def test_several_files_per_kind_are_accepted(self):
        data = form_data()
        data['doc_cheque_book'] = [upload(make_image(), 'a.jpg'), upload(make_image('PNG'), 'b.png', 'image/png')]
        data['doc_national_card'] = [upload(make_image('WEBP'), 'c.webp', 'image/webp')]
        self.client.post(self.url, data)
        self.assertEqual(sorted(d.kind for d in ChequeCreditDocument.objects.all()), ['cheque_book', 'cheque_book', 'national_card'])

    def test_errors_are_shown_with_the_entered_values_and_nothing_is_saved(self):
        response = self.client.post(self.url, post_data(iban='123', requested_limit='abc', bank_name='بانک من'))
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'شماره شبا باید ۲۴ رقم باشد', status_code=400)
        self.assertContains(response, 'value="بانک من"', status_code=400)
        self.assertContains(response, 'value="abc"', status_code=400)
        self.assertEqual(ChequeCreditRequest.objects.count(), 0)

    def test_a_missing_cheque_book_image_is_reported(self):
        data = form_data()
        data['doc_national_card'] = [upload(make_image(), 'c.jpg')]
        response = self.client.post(self.url, data)
        self.assertContains(response, 'تصویر دسته‌چک الزامی است', status_code=400)
        self.assertContains(response, 'تصاویر را دوباره انتخاب کنید', status_code=400)

    def test_a_pdf_is_refused(self):
        data = form_data()
        data['doc_cheque_book'] = [upload(b'%PDF-1.4 x', 'x.pdf', 'application/pdf')]
        response = self.client.post(self.url, data)
        self.assertContains(response, 'PDF پذیرفته نمی‌شود', status_code=400)
        self.assertEqual(ChequeCreditRequest.objects.count(), 0)

    def test_a_second_post_while_pending_is_refused_with_a_message(self):
        self.client.post(self.url, post_data())
        response = self.client.post(self.url, post_data(), follow=True)
        self.assertContains(response, 'در انتظار بررسی')
        self.assertEqual(ChequeCreditRequest.objects.count(), 1)

    def test_the_pending_state_hides_the_form(self):
        self.submit_via_service()
        page = self.client.get(self.url)
        self.assertNotContains(page, 'id="credit-form"')
        self.assertContains(page, 'data-testid="credit-pending"')

    def test_an_ineligible_post_changes_nothing(self):
        cheque_customer = make_user('09120000810', price_level=1)
        client = Client()
        client.force_login(cheque_customer)
        response = client.post(self.url, post_data())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ChequeCreditRequest.objects.count(), 0)

    def test_the_rate_limit_answers_429(self):
        with mock.patch('accounts.cheque_credit_service._rate_limited', return_value=True):
            response = self.client.post(self.url, post_data())
        self.assertEqual(response.status_code, 429)
        self.assertEqual(ChequeCreditRequest.objects.count(), 0)

    def test_an_invalid_checksum_national_code_is_refused_on_post(self):
        CustomUser.objects.filter(pk=self.user.pk).update(national_code='1234567890')
        response = self.client.post(self.url, post_data())
        self.assertEqual(ChequeCreditRequest.objects.count(), 0)
        self.assertNotEqual(response.status_code, 201)

    def test_user_text_is_escaped_on_the_status_card(self):
        self.submit_via_service(business_name='<script>alert(1)</script>')
        page = self.client.get(self.url)
        self.assertNotContains(page, '<script>alert(1)</script>')
        self.assertContains(page, '&lt;script&gt;')


class CancelViewTests(ViewBase):
    def test_the_owner_can_cancel_and_then_apply_again(self):
        request = self.submit_via_service()
        cancel = reverse('accounts:cheque_credit_cancel', args=[request.public_id])
        response = self.client.post(cancel)
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.assertEqual(self.reload(request).status, 'canceled')
        page = self.client.get(self.url)
        self.assertContains(page, 'id="credit-form"')
        self.assertContains(page, 'انصراف مشتری')                               # در سابقه

    def test_cancel_is_post_only(self):
        request = self.submit_via_service()
        self.assertEqual(self.client.get(reverse('accounts:cheque_credit_cancel', args=[request.public_id])).status_code, 405)
        self.assertEqual(self.reload(request).status, 'pending')

    def test_someone_elses_request_is_a_404(self):
        request = self.submit_via_service()
        other = Client()
        other.force_login(make_user('09120000820', price_level=2))
        self.assertEqual(other.post(reverse('accounts:cheque_credit_cancel', args=[request.public_id])).status_code, 404)
        self.assertEqual(self.reload(request).status, 'pending')

    def test_cancelling_a_decided_request_shows_an_error_and_changes_nothing(self):
        request = self.submit_via_service()
        service.reject_request(request, self.admin, 'ناخوانا')
        response = self.client.post(reverse('accounts:cheque_credit_cancel', args=[request.public_id]), follow=True)
        self.assertContains(response, 'قبلاً')
        self.assertEqual(self.reload(request).status, 'rejected')


class DocumentViewTests(ViewBase):
    def setUp(self):
        super().setUp()
        self.request = self.submit_via_service()
        self.document = self.request.documents.get()
        self.url_doc = reverse('accounts:cheque_credit_document', args=[self.document.public_id])

    def test_the_owner_gets_the_image_with_hardened_headers(self):
        response = self.client.get(self.url_doc)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/jpeg')
        self.assertTrue(response['Content-Disposition'].startswith('inline'))
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertIn('sandbox', response['Content-Security-Policy'])
        self.assertIn('no-store', response['Cache-Control'])
        Image.open(io.BytesIO(b''.join(response.streaming_content))).verify()

    def test_everyone_else_gets_a_404_even_staff(self):
        for user in (make_user('09120000830', price_level=2), self.admin):
            other = Client()
            other.force_login(user)
            self.assertEqual(other.get(self.url_doc).status_code, 404)
        import uuid
        self.assertEqual(self.client.get(reverse('accounts:cheque_credit_document', args=[uuid.uuid4()])).status_code, 404)

    def test_a_purged_document_is_a_404_and_the_page_says_so(self):
        service.reject_request(self.request, self.admin, 'x')
        ChequeCreditRequest.objects.filter(pk=self.request.pk).update(decided_at=timezone.now() - timedelta(days=60))
        with self.captureOnCommitCallbacks(execute=True):
            service.purge_expired_documents()
        self.assertEqual(self.client.get(self.url_doc).status_code, 404)
        self.assertContains(self.client.get(self.url), 'پاک شده‌اند')

    def test_the_pending_card_links_the_thumbnails_through_the_secure_view(self):
        page = self.client.get(reverse('accounts:cheque_credit'))
        self.assertContains(page, self.url_doc)
        self.assertNotContains(page, '/media/')                                 # مسیر عمومی فایل هرگز در صفحه نیست


class HistoryTests(ViewBase):
    def test_the_history_shows_the_rejection_reason_escaped_and_the_approval_note(self):
        first = self.submit_via_service()
        service.reject_request(first, self.admin, '<b>مدارک</b> ناقص')
        second = self.submit_via_service()
        with self.captureOnCommitCallbacks(execute=True):
            service.approve_request(second, self.admin, approved_limit='20,000,000')
        page = self.client.get(self.url)
        self.assertContains(page, 'علت رد')
        self.assertContains(page, '&lt;b&gt;مدارک&lt;/b&gt; ناقص')
        self.assertNotContains(page, '<b>مدارک</b>')
        self.assertContains(page, 'مجوز خرید چکی برای حساب شما فعال شد')
        self.assertContains(page, '20,000,000')
        self.assertContains(page, 'data-testid="credit-permitted"')

    def test_another_users_requests_are_never_listed(self):
        other = make_user('09120000840', price_level=2)
        self.submit_via_service(user=other, business_name='فروشگاه دیگری')
        self.assertNotContains(self.client.get(self.url), 'فروشگاه دیگری')


class NavAndCheckoutTests(MediaIsolation, CheckoutTestBase):
    def setUp(self):
        super().setUp()
        CustomUser.objects.filter(pk=self.user.pk).update(price_level=2)
        self.user.refresh_from_db()
        from accounts.tests_cheque_credit import valid_national_code
        CustomUser.objects.filter(pk=self.user.pk).update(national_code=valid_national_code())
        self.user.refresh_from_db()
        self.admin = make_user('09120000850', is_staff=True, is_superuser=True)

    def pending(self):
        with self.captureOnCommitCallbacks(execute=True):
            return service.submit_request(self.user, form_data(), [('cheque_book', upload(make_image()))])

    def test_the_checkout_link_points_to_the_real_page(self):
        page = self.client.get(reverse('orders:checkout'))
        self.assertContains(page, 'data-testid="request-check-link"')
        self.assertContains(page, reverse('accounts:cheque_credit'))
        self.assertNotContains(page, 'soon/check-request')

    def test_a_pending_request_replaces_the_link_with_its_status(self):
        self.pending()
        page = self.client.get(reverse('orders:checkout'))
        self.assertContains(page, 'data-testid="request-check-pending"')
        self.assertNotContains(page, 'data-testid="request-check-link"')
        self.assertContains(page, 'درخواست خرید چکی شما در حال بررسی است')

    def test_after_approval_the_cheque_option_appears_and_the_link_is_gone(self):
        request = self.pending()
        with self.captureOnCommitCallbacks(execute=True):
            service.approve_request(request, self.admin)
        page = self.client.get(reverse('orders:checkout'))
        self.assertNotContains(page, 'request-check-link')
        self.assertNotContains(page, 'request-check-pending')
        self.assertIn('value="check"', page.content.decode())

    def test_choosing_the_request_option_goes_to_the_page_and_keeps_the_cart(self):
        before = CartItem.objects.count()
        response = self.post_order({'address_id': self.address.pk, 'payment_method': 'request_check', 'expected_total': None})
        self.assertRedirects(response, reverse('accounts:cheque_credit'), fetch_redirect_response=False)
        self.assertEqual(CartItem.objects.count(), before)

    def test_the_panel_menu_shows_the_item_only_for_those_who_can_apply(self):
        dashboard = reverse('accounts:dashboard')
        self.assertContains(self.client.get(dashboard), reverse('accounts:cheque_credit'))
        CustomUser.objects.filter(pk=self.user.pk).update(can_purchase_with_check=True)
        self.assertNotContains(self.client.get(dashboard), reverse('accounts:cheque_credit'))
        cheque_customer = make_user('09120000851', price_level=1)
        client = Client()
        client.force_login(cheque_customer)
        self.assertNotContains(client.get(dashboard), reverse('accounts:cheque_credit'))
