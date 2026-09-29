"""
تست‌های Loyalty Phase 6B: نمایش برآورد امتیاز باشگاه در فاکتور چک‌اوت
(templates/orders/partials/invoice.html + loyalty/templatetags/loyalty_tags.py).

عمداً از CheckoutTestBase موجود (orders/tests.py) ارث می‌برد - کاربر/آدرس/سبدِ آماده را دوباره
نمی‌سازد. هیچ‌کدام از این تست‌ها به loyalty/services.py وابسته نیستند - فقط رندر تمپلیت را می‌سنجند.
"""

from django.urls import reverse
from django.utils import timezone

from orders.tests import CheckoutTestBase
from products.models import SiteSettings

INVOICE = 'orders:update_invoice'


class InvoiceLoyaltyPointsDisplayTests(CheckoutTestBase):
    def _get_invoice(self):
        return self.client.get(reverse(INVOICE), {'payment_method': 'check', 'address_id': self.address.pk})

    def test_points_box_is_hidden_when_club_is_not_activated(self):
        response = self._get_invoice()
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'امتیاز باشگاه مشتریان این سفارش')

    def test_points_box_shows_amount_based_estimate(self):
        self.set_policy(
            loyalty_activated_at=timezone.now() - timezone.timedelta(days=1),
            loyalty_mode=SiteSettings.LOYALTY_MODE_AMOUNT, loyalty_amount_step=100000,
        )
        response = self._get_invoice()
        self.assertContains(response, 'امتیاز باشگاه مشتریان این سفارش')
        # سبد: ۲ عدد کالای ۱۰۰,۰۰۰ تومانی (سطح قیمت ۱) = ۲۰۰,۰۰۰ ÷ گام ۱۰۰,۰۰۰ = ۲ امتیاز
        self.assertContains(response, '2 امتیاز')

    def test_points_box_shows_flat_value_in_order_count_mode(self):
        self.set_policy(
            loyalty_activated_at=timezone.now() - timezone.timedelta(days=1),
            loyalty_mode=SiteSettings.LOYALTY_MODE_ORDER_COUNT, loyalty_points_per_order=150,
        )
        response = self._get_invoice()
        self.assertContains(response, 'امتیاز باشگاه مشتریان این سفارش')
        self.assertContains(response, '150 امتیاز')
