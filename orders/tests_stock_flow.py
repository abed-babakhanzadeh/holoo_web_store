"""
رزرو موجودی در مسیر ثبت سفارش و پرداخت: رزرو اتمیک هنگام ثبت (سفارش آنلاین ۲۰ دقیقه مهلت، چکی بی‌مهلت)، شکست شفاف بدون
اثر جانبی، آزادسازی با لغو، تأیید رزرو با پرداخت (و دوباره‌رزرو/رد وقتی مهلت در درگاه گذشته)، و شروع پرداخت.
"""
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from accounts.testing import make_approved_user
from cart.models import Cart, CartItem
from orders.models import Order
from orders.tests import CheckoutTestBase
from payments.models import Transaction
from payments.signals import payment_succeeded
from products import stock
from products.models import Product, StockReservation
from products.tasks import expire_stock_reservations
from promotions.models import CouponRedemption
from promotions.testing import make_coupon


class StockFlowBase(CheckoutTestBase):
    def submit(self, method='check', **extra):
        data = {'address_id': self.address.pk, 'payment_method': method}
        data.update(extra)
        with self.captureOnCommitCallbacks(execute=True):
            return self.post_order(data)

    def reload(self, product=None):
        product = product or self.product
        product.refresh_from_db()
        return product

    def row(self, order):
        return StockReservation.objects.get(order_id=order.id, product=self.product)

    def paid(self, order):
        txn = Transaction.objects.create(user=order.user, order=order, amount=order.total_price, status='success',
                                         authority=f'AUTH-STK-{Transaction.objects.count() + 1}', ref_id='R1')
        payment_succeeded.send(sender=Transaction, order=order, transaction=txn)
        return txn


class SubmitReservesStockTests(StockFlowBase):
    def test_cheque_order_reserves_without_a_deadline(self):
        response = self.submit('check')
        self.assertEqual(response.status_code, 302)
        order = Order.objects.get(user=self.user)
        row = self.row(order)
        self.assertEqual((row.quantity, row.state, row.expires_at), (2, 'held', None))
        self.assertEqual(self.reload().reserved_quantity, 2)
        self.assertEqual(self.reload().available_quantity, 8)

    def test_online_order_gets_a_twenty_minute_payment_window(self):
        before = timezone.now()
        self.submit('cash')
        order = Order.objects.get(user=self.user)
        deadline = self.row(order).expires_at
        self.assertGreaterEqual(deadline, before + timedelta(minutes=19, seconds=50))
        self.assertLessEqual(deadline, timezone.now() + timedelta(minutes=20, seconds=5))

    def test_registering_an_order_does_not_touch_the_holoo_stock_column(self):
        self.submit('check')
        self.assertEqual(self.reload().stock, 10)                       # ستون مالِ هلو است؛ فقط reserved_quantity تغییر می‌کند

    def test_shortage_blocks_the_order_with_a_clear_message_and_no_side_effects(self):
        Product.objects.filter(pk=self.product.pk).update(stock=1)
        response = self.submit('check')
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, 'موجودی کالاهای زیر', status_code=409)
        self.assertContains(response, 'حداکثر 1 عدد قابل‌سفارش است', status_code=409)
        self.assertContains(response, 'کالا', status_code=409)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(StockReservation.objects.count(), 0)
        self.assertEqual(self.reload().reserved_quantity, 0)
        self.assertTrue(Cart.objects.filter(user=self.user).exists())    # سبد دست‌نخورده
        self.assertEqual(CartItem.objects.get(cart=self.cart).quantity, 2)

    def test_out_of_stock_product_message_says_it_became_unavailable(self):
        Product.objects.filter(pk=self.product.pk).update(stock=0)
        response = self.submit('check')
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, 'ناموجود شد', status_code=409)

    def test_shortage_does_not_burn_a_coupon_reservation(self):
        make_coupon('STK1', value=10, total_limit=1, per_user_limit=None)
        session = self.client.session
        from promotions import coupons
        session[coupons.SESSION_KEY] = 'STK1'
        session.save()
        Product.objects.filter(pk=self.product.pk).update(stock=1)
        response = self.submit('check', expected_total=180000)
        self.assertContains(response, 'حداکثر 1 عدد', status_code=409)
        self.assertEqual(CouponRedemption.objects.count(), 0)
        self.assertEqual(Order.objects.count(), 0)

    def test_second_buyer_of_the_last_units_is_refused_after_the_first(self):
        Product.objects.filter(pk=self.product.pk).update(stock=3)
        self.assertEqual(self.submit('check').status_code, 302)
        other_cart = Cart.objects.create(user=self.other)
        CartItem.objects.create(cart=other_cart, product=self.product, quantity=2)
        address = self.make_address(self.other, self.post_city)
        self.client.force_login(self.other)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post_order({'address_id': address.pk, 'payment_method': 'check'})
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, 'حداکثر 1 عدد', status_code=409)
        self.assertEqual(self.reload().reserved_quantity, 2)

    def test_the_safety_buffer_is_respected_at_submit(self):
        self.set_policy(stock_safety_buffer=2)
        Product.objects.filter(pk=self.product.pk).update(stock=3)       # قابل‌فروش 1 < 2 درخواستی
        self.assertEqual(self.submit('check').status_code, 409)

    def test_cart_items_of_the_same_product_with_different_colors_reserve_the_summed_quantity(self):
        from products.models import ProductColor
        red = ProductColor.objects.create(product=self.product, name='قرمز', hex_code='#ff0000')
        blue = ProductColor.objects.create(product=self.product, name='آبی', hex_code='#0000ff')
        CartItem.objects.all().delete()
        CartItem.objects.create(cart=self.cart, product=self.product, color=red, quantity=2)
        CartItem.objects.create(cart=self.cart, product=self.product, color=blue, quantity=3)
        self.assertEqual(self.submit('check').status_code, 302)
        order = Order.objects.get(user=self.user)
        self.assertEqual(StockReservation.objects.filter(order_id=order.id).count(), 1)
        self.assertEqual(self.row(order).quantity, 5)
        self.assertEqual(self.reload().reserved_quantity, 5)


class ReleaseAndPaymentTests(StockFlowBase):
    def place(self, method='cash'):
        self.submit(method)
        return Order.objects.get(user=self.user)

    def test_canceling_the_order_releases_the_reservation(self):
        order = self.place()
        with self.captureOnCommitCallbacks(execute=True):
            order.status = 'canceled'
            order.save()
        self.assertEqual(self.row(order).state, 'released')
        self.assertEqual(self.reload().reserved_quantity, 0)

    def test_successful_payment_removes_the_deadline_and_keeps_the_hold_until_the_admin_decides(self):
        order = self.place('cash')
        self.assertIsNotNone(self.row(order).expires_at)
        self.paid(order)
        row = self.row(order)
        self.assertEqual((row.state, row.expires_at), ('held', None))
        self.assertEqual(stock.expire_stale(now=timezone.now() + timedelta(hours=5)), 0)
        self.assertEqual(self.reload().reserved_quantity, 2)

    def test_payment_after_the_window_re_reserves_when_stock_is_still_there(self):
        order = self.place('cash')
        StockReservation.objects.filter(order_id=order.id).update(expires_at=timezone.now() - timedelta(minutes=1))
        stock.expire_stale()
        self.assertEqual(self.reload().reserved_quantity, 0)
        self.paid(order)
        self.assertEqual((self.row(order).state, self.row(order).expires_at), ('held', None))
        self.assertEqual(self.reload().reserved_quantity, 2)
        self.assertEqual(Order.objects.get(pk=order.pk).status, 'pending')

    def test_payment_after_the_window_with_no_stock_left_rejects_the_order_and_alerts_the_admin(self):
        order = self.place('cash')
        StockReservation.objects.filter(order_id=order.id).update(expires_at=timezone.now() - timedelta(minutes=1))
        stock.expire_stale()
        Product.objects.filter(pk=self.product.pk).update(stock=1)           # کس دیگری/فروش حضوری موجودی را برد
        with mock.patch('notifications.service.notify_admin') as alert:
            self.paid(order)
        order.refresh_from_db()
        self.assertEqual(order.status, 'rejected_stock')
        self.assertEqual(order.customer_status, 'stock_issue')
        self.assertTrue(order.is_paid)                                        # پول گرفته شده؛ بازگشت وجه تصمیم دستی مدیر است
        self.assertEqual(self.reload().reserved_quantity, 0)
        alert.assert_called_once()
        self.assertIn('بازگشت وجه با تصمیم دستی', alert.call_args.kwargs['message'])

    def test_payment_on_an_already_canceled_order_never_creates_a_new_reservation(self):
        order = self.place('cash')
        with self.captureOnCommitCallbacks(execute=True):
            order.status = 'canceled'
            order.save()
        self.paid(order)
        self.assertEqual(self.reload().reserved_quantity, 0)
        self.assertEqual(StockReservation.objects.get(order_id=order.id).state, 'released')

    def test_expiry_task_expires_unpaid_holds_but_never_paid_or_cheque_ones(self):
        online = self.place('cash')
        StockReservation.objects.filter(order_id=online.id).update(expires_at=timezone.now() - timedelta(minutes=1))
        result = expire_stock_reservations()
        self.assertIn('expired=1', result)
        self.assertEqual(self.row(online).state, 'expired')

    def test_rejecting_for_stock_is_idempotent(self):
        from orders.stock_hooks import reject_for_stock
        order = self.place('cash')
        with mock.patch('notifications.service.notify_admin') as alert:
            self.assertTrue(reject_for_stock(order, 'x'))
            self.assertFalse(reject_for_stock(order, 'x'))
        alert.assert_called_once()


class PaymentStartTests(StockFlowBase):
    def start(self, order):
        return self.client.post(reverse('payments:start_payment', args=[order.id]), {'wallet_amount': '0'})

    def place(self):
        self.submit('cash')
        return Order.objects.get(user=self.user)

    def test_starting_payment_restarts_the_twenty_minute_window(self):
        order = self.place()
        StockReservation.objects.filter(order_id=order.id).update(expires_at=timezone.now() + timedelta(minutes=1))
        response = self.start(order)
        self.assertEqual(response.status_code, 302)
        self.assertGreater(self.row(order).expires_at, timezone.now() + timedelta(minutes=19))

    def test_starting_payment_after_expiry_re_reserves_when_possible(self):
        order = self.place()
        StockReservation.objects.filter(order_id=order.id).update(expires_at=timezone.now() - timedelta(minutes=1))
        stock.expire_stale()
        response = self.start(order)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.row(order).state, 'held')
        self.assertEqual(self.reload().reserved_quantity, 2)
        self.assertEqual(Transaction.objects.filter(order=order, status='pending').count(), 1)

    def test_starting_payment_after_expiry_without_stock_cancels_the_order_before_the_gateway(self):
        order = self.place()
        StockReservation.objects.filter(order_id=order.id).update(expires_at=timezone.now() - timedelta(minutes=1))
        stock.expire_stale()
        Product.objects.filter(pk=self.product.pk).update(stock=1)
        response = self.start(order)
        self.assertRedirects(response, reverse('orders:order_detail_full', args=[order.id]), fetch_redirect_response=False)
        order.refresh_from_db()
        self.assertEqual(order.status, 'canceled')
        self.assertIn('نبود موجودی', order.cancel_reason)
        self.assertFalse(Transaction.objects.filter(order=order).exists())          # به درگاه نرفت و کیف‌پول کسر نشد
        self.assertEqual(self.reload().reserved_quantity, 0)


class CustomerFacingStatusTests(StockFlowBase):
    def test_paid_order_shows_waiting_for_admin_then_preparing_after_approval(self):
        self.submit('cash')
        order = Order.objects.get(user=self.user)
        self.paid(order)
        detail = self.client.get(reverse('orders:order_detail_full', args=[order.id]))
        self.assertContains(detail, 'در انتظار تأیید مدیر / در حال بررسی')
        history = self.client.get(reverse('orders:order_history'))
        self.assertContains(history, 'در انتظار تأیید مدیر / در حال بررسی')
        Order.objects.filter(pk=order.pk).update(approved_at=timezone.now())
        detail = self.client.get(reverse('orders:order_detail_full', args=[order.id]))
        self.assertContains(detail, 'در حال آماده‌سازی انبار')
        self.assertNotContains(detail, 'در انتظار تأیید مدیر')

    def test_cheque_order_shows_waiting_for_admin_not_waiting_for_payment(self):
        self.submit('check')
        order = Order.objects.get(user=self.user)
        detail = self.client.get(reverse('orders:order_detail_full', args=[order.id]))
        self.assertContains(detail, 'در انتظار تأیید مدیر / در حال بررسی')
        self.assertNotContains(detail, 'پرداخت آنلاین سفارش')

    def test_stock_rejected_order_is_shown_as_needing_coordination(self):
        self.submit('cash')
        order = Order.objects.get(user=self.user)
        Order.objects.filter(pk=order.pk).update(status='rejected_stock')
        detail = self.client.get(reverse('orders:order_detail_full', args=[order.id]))
        self.assertContains(detail, 'نیازمند هماهنگی (اتمام موجودی)')
