"""
تست‌های Order.delivered_at - مبدأ مهلت ۷روزه‌ی مرجوعی کالا (Phase 1 - Part A).
پرشدنش نباید به هیچ فرآیند دیگری (تسویه‌ی کیف‌پول، ثبت سفارش، ادمین سفارش) خللی وارد کند.
"""

from unittest import mock

from django.contrib.admin.sites import AdminSite
from django.test import TestCase

from accounts.models import CustomUser
from orders.admin import OrderAdmin
from orders.models import Order
from products.models import Category, Product


class DeliveredAtTests(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(phone_number='09120009001', first_name='رضا')

    def make_order(self, **overrides):
        data = dict(
            user=self.user, first_name='رضا', last_name='ی', phone='09120009001',
            address='تهران', payment_method='cash', shipping_cost=0, total_price=100000, status='processing',
        )
        data.update(overrides)
        return Order.objects.create(**data)

    def test_delivered_at_is_empty_before_delivery(self):
        order = self.make_order()
        self.assertIsNone(order.delivered_at)

    def test_first_transition_to_delivered_sets_the_timestamp(self):
        order = self.make_order()
        order.status = 'delivered'
        order.save()
        order.refresh_from_db()
        self.assertIsNotNone(order.delivered_at)

    def test_creating_directly_with_delivered_status_also_sets_it(self):
        order = self.make_order(status='delivered')
        self.assertIsNotNone(order.delivered_at)

    def test_resaving_an_already_delivered_order_does_not_move_the_timestamp(self):
        """ مبدأ مهلت ۷روزه نباید با هر ذخیره‌ی بعدی (مثلاً تغییر آدرس در ادمین) جلو برود """
        order = self.make_order()
        order.status = 'delivered'
        order.save()
        order.refresh_from_db()
        first_timestamp = order.delivered_at

        order.address = 'آدرس تغییریافته'
        order.save()
        order.refresh_from_db()
        self.assertEqual(order.delivered_at, first_timestamp)

    def test_going_back_to_shipped_and_delivered_again_does_not_move_the_timestamp(self):
        """ اگر به اشتباه وضعیت برگردد و دوباره «تحویل داده شده» بشود، مهلت نباید تازه شود """
        order = self.make_order()
        order.status = 'delivered'
        order.save()
        order.refresh_from_db()
        first_timestamp = order.delivered_at

        order.status = 'shipped'
        order.save()
        order.status = 'delivered'
        order.save()
        order.refresh_from_db()
        self.assertEqual(order.delivered_at, first_timestamp)

    def test_update_fields_save_still_persists_delivered_at(self):
        """
        اگر caller با update_fields محدود صدا بزند (مثل تسک‌های holoo)، delivered_at باید به همان
        لیست اضافه شود وگرنه جنگو آن را در دیتابیس نمی‌نویسد - نگاه کنید orders/models.py:Order.save
        """
        order = self.make_order()
        order.status = 'delivered'
        order.save(update_fields=['status', 'updated_at'])
        order.refresh_from_db()
        self.assertIsNotNone(order.delivered_at)

    def test_other_status_transitions_never_touch_delivered_at(self):
        order = self.make_order()
        for status in ('shipped', 'canceled'):
            with self.subTest(status=status):
                order.status = status
                order.save()
                order.refresh_from_db()
                self.assertIsNone(order.delivered_at)

    def test_admin_status_change_to_delivered_sets_the_timestamp_without_breaking_shipped_notification(self):
        """
        رگرسیون: OrderAdmin.save_model پیامک کد رهگیری را فقط بر مبنای status/tracking_code
        می‌فرستد؛ افزودن delivered_at نباید آن منطق را تحت تأثیر قرار دهد.
        """
        order = self.make_order(status='shipped', tracking_code='POST-999')
        admin_instance = OrderAdmin(Order, AdminSite())
        form = mock.Mock(changed_data=['status'])

        order.status = 'delivered'
        with mock.patch('notifications.service.notify') as notify_mock:
            admin_instance.save_model(request=mock.Mock(), obj=order, form=form, change=True)

        order.refresh_from_db()
        self.assertIsNotNone(order.delivered_at)
        self.assertEqual(notify_mock.call_count, 0)   # status دیگر 'shipped' نیست، پس پیامک کد رهگیری نباید برود


class DeliveredAtDoesNotAffectUnrelatedFlowsTests(TestCase):
    """ افزودن delivered_at و تغییرات فرم برداشت کیف‌پول نباید در مسیرهای بی‌ربط اثر بگذارد """

    def setUp(self):
        self.user = CustomUser.objects.create_user(phone_number='09120009002', first_name='مریم')
        self.category = Category.objects.create(name='تست', slug='delivered-at-regression-cat')
        self.product = Product.objects.create(
            name='کالا', slug='delivered-at-regression-product', erp_code='ERP-DELIVERED-AT-1',
            category=self.category, price=100000, stock=5,
        )

    def test_creating_a_normal_pending_order_is_unaffected(self):
        order = Order.objects.create(
            user=self.user, first_name='مریم', last_name='ی', phone='09120009002',
            address='تهران', payment_method='cash', shipping_cost=0, total_price=100000,
        )
        self.assertEqual(order.status, 'pending')
        self.assertIsNone(order.delivered_at)

    def test_wallet_withdraw_form_still_works_without_any_saved_bank_account(self):
        """
        کاربری که هنوز هیچ UserBankAccount ندارد باید بتواند مثل قبل با پرکردن دستی فیلدها
        درخواست برداشت ثبت کند - رگرسیون روی رفتار قبل از افزودن کمبوباکس.
        """
        from wallet.forms import WithdrawalRequestForm
        from wallet.models import Wallet
        from wallet import services

        wallet = Wallet.objects.create(user=self.user, balance=200000)
        form = WithdrawalRequestForm(
            {
                'amount': '100000', 'iban': 'IR620570123456789012345678',
                'card_number': '6037991234567890', 'account_holder': 'مریم رضایی',
            },
            wallet=wallet,
        )
        self.assertTrue(form.is_valid(), form.errors)
        account = form.resolve_bank_account()
        self.assertEqual(account.card_number, '6037991234567890')
        self.assertTrue(account.is_default)   # اولین حساب کاربر، خودکار پیش‌فرض می‌شود

        services.reserve_withdrawal(
            wallet, form.cleaned_data['amount'], account_holder=account.account_holder_full_name,
            card_number=account.card_number, iban=account.iban,
        )
        self.assertEqual(wallet.withdrawal_requests.count(), 1)
