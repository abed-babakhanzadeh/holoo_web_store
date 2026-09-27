"""
تست‌های Part C.1: ویزارد سه‌مرحله‌ای مرجوعی (returns/views.py) - گاردهای مالکیت/مهلت/ظرفیت،
چرخه‌ی سشن، و ثبت موفق با هر دو روش بازپرداخت.
"""

from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserBankAccount
from orders.models import Order
from products.models import SiteSettings
from returns.models import ReturnRequest
from returns.tests import ReturnsTestMixin
from returns.views import SESSION_KEY
from wallet.models import Wallet, WalletTransaction


class ReturnWizardTestBase(ReturnsTestMixin, TestCase):
    def setUp(self):
        self.customer = self.make_user()
        self.category = self.make_category()
        self.product = self.make_product(self.category)
        self.reason = self.make_reason()
        self.client.force_login(self.customer)
        self.addCleanup(cache.delete, SiteSettings.CACHE_KEY)

    def make_deliverable_order(self, quantity=3):
        order = self.make_order(self.customer)
        item = self.make_order_item(order, self.product, price=50000, quantity=quantity)
        return order, item

    def urls(self, order_id):
        return {
            'step1': reverse('returns:wizard_step1', args=[order_id]),
            'step2': reverse('returns:wizard_step2', args=[order_id]),
            'step3': reverse('returns:wizard_step3', args=[order_id]),
        }


class OwnershipGuardTests(ReturnWizardTestBase):
    def test_other_users_order_is_not_accessible(self):
        stranger = self.make_user('09160009001')
        order, _item = self.make_deliverable_order()
        order.user = stranger
        order.save(update_fields=['user'])

        response = self.client.get(self.urls(order.id)['step1'])
        self.assertEqual(response.status_code, 404)

    def test_nonexistent_order_is_404(self):
        response = self.client.get(self.urls(999999)['step1'])
        self.assertEqual(response.status_code, 404)

    def test_anonymous_user_is_redirected_to_login(self):
        self.client.logout()
        order, _item = self.make_deliverable_order()
        response = self.client.get(self.urls(order.id)['step1'])
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response.url)


class DeliveryAndDeadlineGuardTests(ReturnWizardTestBase):
    def test_non_delivered_order_redirects_to_order_detail(self):
        order, _item = self.make_deliverable_order()
        order.status = 'processing'
        order.save(update_fields=['status'])

        response = self.client.get(self.urls(order.id)['step1'])
        self.assertRedirects(response, reverse('orders:order_detail_full', args=[order.id]))

    def test_expired_return_window_redirects_to_order_detail(self):
        settings_obj = SiteSettings.load()
        settings_obj.return_period_days = 7
        settings_obj.return_period_unit = SiteSettings.RETURN_PERIOD_UNIT_CALENDAR_DAYS
        settings_obj.save()

        order, _item = self.make_deliverable_order()
        Order.objects.filter(pk=order.pk).update(delivered_at=timezone.now() - timedelta(days=30))

        response = self.client.get(self.urls(order.id)['step1'])
        self.assertRedirects(response, reverse('orders:order_detail_full', args=[order.id]))

    def test_delivered_order_within_window_shows_step_one(self):
        order, _item = self.make_deliverable_order()
        response = self.client.get(self.urls(order.id)['step1'])
        self.assertEqual(response.status_code, 200)


class QuantityCapGuardTests(ReturnWizardTestBase):
    def test_form_rejects_quantity_above_returnable_cap(self):
        order, item = self.make_deliverable_order(quantity=3)
        response = self.client.post(self.urls(order.id)['step1'], {f'quantity_{item.pk}': '4'})
        self.assertEqual(response.status_code, 200)   # فرم دوباره با خطا نشان داده می‌شود
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_form_rejects_when_nothing_is_selected(self):
        order, item = self.make_deliverable_order(quantity=3)
        response = self.client.post(self.urls(order.id)['step1'], {f'quantity_{item.pk}': '0'})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(SESSION_KEY, self.client.session)

    def test_form_accepts_quantity_at_exactly_the_cap(self):
        order, item = self.make_deliverable_order(quantity=3)
        response = self.client.post(self.urls(order.id)['step1'], {f'quantity_{item.pk}': '3'})
        self.assertRedirects(response, self.urls(order.id)['step2'])

    def test_cap_shrinks_after_an_existing_active_request(self):
        """ سقفِ فرم باید ظرفیتِ *باقیمانده* را در نظر بگیرد، نه کل تعداد خریداری‌شده """
        from returns.services import create_return_request
        order, item = self.make_deliverable_order(quantity=3)
        create_return_request(
            order, self.customer, [{'order_item': item, 'reason': self.reason, 'requested_quantity': 2}],
            refund_method=ReturnRequest.REFUND_WALLET,
        )
        response = self.client.post(self.urls(order.id)['step1'], {f'quantity_{item.pk}': '2'})
        self.assertEqual(response.status_code, 200)   # فقط ۱ واحد باقی مانده؛ ۲ باید رد شود
        self.assertNotIn(SESSION_KEY, self.client.session)


class SessionLifecycleTests(ReturnWizardTestBase):
    def test_step_two_redirects_to_step_one_without_session_data(self):
        order, _item = self.make_deliverable_order()
        response = self.client.get(self.urls(order.id)['step2'])
        self.assertRedirects(response, self.urls(order.id)['step1'])

    def test_step_three_redirects_to_step_one_without_any_session_data(self):
        order, _item = self.make_deliverable_order()
        response = self.client.get(self.urls(order.id)['step3'])
        self.assertRedirects(response, self.urls(order.id)['step1'])

    def test_step_three_redirects_to_step_one_when_only_step_one_data_present(self):
        order, item = self.make_deliverable_order(quantity=2)
        self.client.post(self.urls(order.id)['step1'], {f'quantity_{item.pk}': '1'})
        response = self.client.get(self.urls(order.id)['step3'])
        self.assertRedirects(response, self.urls(order.id)['step1'])

    def test_session_data_is_scoped_to_its_own_order(self):
        """ داده‌ی سشن مربوط به سفارش دیگر نباید برای این سفارش قابل استفاده باشد """
        order_a, item_a = self.make_deliverable_order(quantity=2)
        order_b, _item_b = self.make_deliverable_order(quantity=2)
        self.client.post(self.urls(order_a.id)['step1'], {f'quantity_{item_a.pk}': '1'})
        response = self.client.get(self.urls(order_b.id)['step2'])
        self.assertRedirects(response, self.urls(order_b.id)['step1'])

    def test_full_wizard_walk_populates_and_then_clears_session(self):
        order, item = self.make_deliverable_order(quantity=2)

        r1 = self.client.post(self.urls(order.id)['step1'], {f'quantity_{item.pk}': '1'})
        self.assertRedirects(r1, self.urls(order.id)['step2'])
        session_data = self.client.session[SESSION_KEY]
        self.assertEqual(session_data['order_id'], order.id)
        self.assertEqual(session_data['step1'], {str(item.pk): 1})

        r2 = self.client.post(self.urls(order.id)['step2'], {
            f'reason_{item.pk}': str(self.reason.pk), f'description_{item.pk}': 'رنگ اشتباه بود',
        })
        self.assertRedirects(r2, self.urls(order.id)['step3'])
        self.assertIn('step2', self.client.session[SESSION_KEY])

        r3 = self.client.post(self.urls(order.id)['step3'], {'refund_method': 'wallet'})
        self.assertEqual(r3.status_code, 302)
        self.assertNotIn(SESSION_KEY, self.client.session)   # سشن بعد از ثبت موفق پاک شده باشد

        request = ReturnRequest.objects.get(order=order)
        self.assertRedirects(r3, reverse('returns:wizard_success', args=[request.pk]))


class SuccessfulSubmissionTests(ReturnWizardTestBase):
    def _walk_to_step_three(self, order, item, quantity=None, description='توضیح تست', reason=None):
        reason = reason or self.reason
        quantity = item.quantity if quantity is None else quantity
        self.client.post(self.urls(order.id)['step1'], {f'quantity_{item.pk}': str(quantity)})
        self.client.post(self.urls(order.id)['step2'], {
            f'reason_{item.pk}': str(reason.pk), f'description_{item.pk}': description,
        })

    def test_wallet_refund_method_creates_return_request(self):
        order, item = self.make_deliverable_order(quantity=2)
        self._walk_to_step_three(order, item)

        response = self.client.post(self.urls(order.id)['step3'], {'refund_method': 'wallet'})
        request = ReturnRequest.objects.get(order=order)
        self.assertEqual(request.refund_method, ReturnRequest.REFUND_WALLET)
        self.assertEqual(request.status, ReturnRequest.STATUS_PENDING)
        self.assertEqual(request.items.get().requested_quantity, 2)
        self.assertRedirects(response, reverse('returns:wizard_success', args=[request.pk]))

    def test_bank_refund_with_new_account_creates_return_request_and_saves_account(self):
        order, item = self.make_deliverable_order(quantity=1)
        self._walk_to_step_three(order, item)

        response = self.client.post(self.urls(order.id)['step3'], {
            'refund_method': 'bank', 'bank_account': 'new', 'card_number': '6037991234567890',
            'account_holder': 'علی رضایی',
        })
        request = ReturnRequest.objects.get(order=order)
        self.assertEqual(request.refund_method, ReturnRequest.REFUND_BANK)
        self.assertEqual(request.bank_card_snapshot, '6037991234567890')
        self.assertTrue(UserBankAccount.objects.filter(user=self.customer, card_number='6037991234567890').exists())
        self.assertRedirects(response, reverse('returns:wizard_success', args=[request.pk]))

    def test_bank_refund_with_saved_account_reuses_its_snapshot(self):
        account = UserBankAccount.objects.create(
            user=self.customer, account_holder_first_name='مریم', account_holder_last_name='کاظمی',
            card_number='6219861234567890',
        )
        order, item = self.make_deliverable_order(quantity=1)
        self._walk_to_step_three(order, item)

        self.client.post(self.urls(order.id)['step3'], {
            'refund_method': 'bank', 'bank_account': str(account.pk),
        })
        request = ReturnRequest.objects.get(order=order)
        self.assertEqual(request.bank_account_id, account.pk)
        self.assertEqual(request.bank_card_snapshot, '6219861234567890')

    def test_reason_requiring_description_is_enforced_in_step_two(self):
        other_reason = self.make_reason(title='سایر', requires_description=True)
        order, item = self.make_deliverable_order(quantity=1)
        self.client.post(self.urls(order.id)['step1'], {f'quantity_{item.pk}': '1'})

        response = self.client.post(self.urls(order.id)['step2'], {
            f'reason_{item.pk}': str(other_reason.pk), f'description_{item.pk}': '',
        })
        self.assertEqual(response.status_code, 200)   # بدون توضیح رد می‌شود
        self.assertNotIn('step2', self.client.session.get(SESSION_KEY, {}))
