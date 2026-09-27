"""
تست‌های returns/services.py (Phase 1 - Part B): ماشین وضعیت، قفل هم‌زمانی create_return_request،
و اسنپ‌شات قطعی مالی در گذار به REFUND_PENDING/COMPLETED.
"""

import threading

from django.db import connection
from django.test import TestCase, TransactionTestCase

from accounts.models import UserBankAccount
from returns.models import ReturnItem, ReturnRequest
from returns.services import (
    InsufficientReturnableQuantityError, InvalidReturnStateError, approve_return_request,
    complete_refund, create_return_request, mark_items_received, mark_refund_pending,
    reject_return_request,
)
from returns.refund_calculator import get_returnable_quantity
from returns.signals import (
    return_approved, return_item_received, return_refund_completed, return_rejected, return_requested,
)
from returns.tests import ReturnsTestMixin
from wallet.models import Wallet, WalletTransaction


class ServiceTestBase(ReturnsTestMixin, TestCase):
    def setUp(self):
        self.user = self.make_user()
        self.category = self.make_category()
        self.order = self.make_order(self.user)
        self.product = self.make_product(self.category)
        self.order_item = self.make_order_item(self.order, self.product, price=50000, quantity=3)
        self.reason = self.make_reason()

    def create_request(self, quantity=1, **kwargs):
        return create_return_request(
            self.order, self.user,
            [{'order_item': self.order_item, 'reason': self.reason, 'requested_quantity': quantity}],
            refund_method=ReturnRequest.REFUND_WALLET, **kwargs,
        )


class CreateReturnRequestTests(ServiceTestBase):
    def test_happy_path_creates_request_and_items(self):
        request = self.create_request(quantity=2)
        self.assertEqual(request.status, ReturnRequest.STATUS_PENDING)
        item = request.items.get()
        self.assertEqual((item.order_item_id, item.requested_quantity), (self.order_item.pk, 2))

    def test_bank_method_snapshots_the_account(self):
        account = UserBankAccount.objects.create(
            user=self.user, account_holder_first_name='علی', account_holder_last_name='رضایی',
            card_number='6037991234567890',
        )
        request = create_return_request(
            self.order, self.user,
            [{'order_item': self.order_item, 'reason': self.reason, 'requested_quantity': 1}],
            refund_method=ReturnRequest.REFUND_BANK, bank_account=account,
        )
        self.assertEqual(request.bank_account_id, account.pk)
        self.assertEqual(request.bank_card_snapshot, '6037991234567890')
        self.assertEqual(request.bank_account_holder_snapshot, 'علی رضایی')

    def test_bank_method_without_account_raises(self):
        with self.assertRaises(ValueError):
            create_return_request(
                self.order, self.user,
                [{'order_item': self.order_item, 'reason': self.reason, 'requested_quantity': 1}],
                refund_method=ReturnRequest.REFUND_BANK,
            )
        self.assertEqual(ReturnRequest.objects.count(), 0)

    def test_no_items_raises(self):
        with self.assertRaises(ValueError):
            create_return_request(self.order, self.user, [], refund_method=ReturnRequest.REFUND_WALLET)

    def test_other_users_order_raises(self):
        stranger = self.make_user('09140009999')
        with self.assertRaises(ValueError):
            create_return_request(
                self.order, stranger,
                [{'order_item': self.order_item, 'reason': self.reason, 'requested_quantity': 1}],
                refund_method=ReturnRequest.REFUND_WALLET,
            )

    def test_requesting_more_than_returnable_capacity_raises_and_creates_nothing(self):
        with self.assertRaises(InsufficientReturnableQuantityError):
            self.create_request(quantity=self.order_item.quantity + 1)
        self.assertEqual(ReturnRequest.objects.count(), 0)
        self.assertEqual(ReturnItem.objects.count(), 0)

    def test_order_item_from_a_different_order_raises(self):
        other_order = self.make_order(self.user)
        with self.assertRaises(ValueError):
            create_return_request(
                other_order, self.user,
                [{'order_item': self.order_item, 'reason': self.reason, 'requested_quantity': 1}],
                refund_method=ReturnRequest.REFUND_WALLET,
            )

    def test_fires_return_requested_signal_after_commit(self):
        received = []
        handler = lambda sender, return_request, **kw: received.append(return_request.pk)
        return_requested.connect(handler, weak=False)
        self.addCleanup(return_requested.disconnect, handler)

        with self.captureOnCommitCallbacks(execute=True):
            request = self.create_request(quantity=1)
        self.assertEqual(received, [request.pk])


class ApproveReturnRequestTests(ServiceTestBase):
    def test_pending_to_approved(self):
        request = self.create_request()
        approved = approve_return_request(request, self.user)
        self.assertEqual(approved.status, ReturnRequest.STATUS_APPROVED)
        self.assertIsNotNone(approved.decided_at)
        self.assertEqual(approved.decided_by_id, self.user.pk)

    def test_wrong_state_raises(self):
        request = self.create_request()
        approve_return_request(request, self.user)
        with self.assertRaises(InvalidReturnStateError):
            approve_return_request(request, self.user)

    def test_fires_signal(self):
        request = self.create_request()
        received = []
        handler = lambda sender, return_request, **kw: received.append(return_request.pk)
        return_approved.connect(handler, weak=False)
        self.addCleanup(return_approved.disconnect, handler)
        with self.captureOnCommitCallbacks(execute=True):
            approve_return_request(request, self.user)
        self.assertEqual(received, [request.pk])


class MarkItemsReceivedTests(ServiceTestBase):
    def setUp(self):
        super().setUp()
        self.request = self.create_request(quantity=2)
        approve_return_request(self.request, self.user)
        self.item = self.request.items.get()

    def test_happy_path_sets_approved_quantity_and_status(self):
        result = mark_items_received(self.request, {self.item.pk: 1}, self.user)
        self.assertEqual(result.status, ReturnRequest.STATUS_ITEM_RECEIVED)
        self.item.refresh_from_db()
        self.assertEqual(self.item.approved_quantity, 1)
        self.assertIsNotNone(result.item_received_at)

    def test_missing_item_in_dict_raises(self):
        with self.assertRaises(ValueError):
            mark_items_received(self.request, {}, self.user)

    def test_unknown_item_id_raises(self):
        with self.assertRaises(ValueError):
            mark_items_received(self.request, {self.item.pk: 1, 999999: 0}, self.user)

    def test_approved_quantity_above_requested_raises(self):
        with self.assertRaises(ValueError):
            mark_items_received(self.request, {self.item.pk: 3}, self.user)

    def test_negative_approved_quantity_raises(self):
        with self.assertRaises(ValueError):
            mark_items_received(self.request, {self.item.pk: -1}, self.user)

    def test_zero_approved_quantity_is_allowed(self):
        """ کارشناس می‌تواند کاملاً رد کند (۰ واحد تأیید) بدون اینکه به REJECTED برود """
        result = mark_items_received(self.request, {self.item.pk: 0}, self.user)
        self.assertEqual(result.status, ReturnRequest.STATUS_ITEM_RECEIVED)

    def test_wrong_state_raises(self):
        mark_items_received(self.request, {self.item.pk: 1}, self.user)
        with self.assertRaises(InvalidReturnStateError):
            mark_items_received(self.request, {self.item.pk: 1}, self.user)

    def test_fires_signal(self):
        received = []
        handler = lambda sender, return_request, **kw: received.append(return_request.pk)
        return_item_received.connect(handler, weak=False)
        self.addCleanup(return_item_received.disconnect, handler)
        with self.captureOnCommitCallbacks(execute=True):
            mark_items_received(self.request, {self.item.pk: 1}, self.user)
        self.assertEqual(received, [self.request.pk])


class MarkRefundPendingTests(ServiceTestBase):
    def setUp(self):
        super().setUp()
        self.request = self.create_request(quantity=2)
        approve_return_request(self.request, self.user)
        self.item = self.request.items.get()

    def test_computes_refund_amount_from_approved_quantity(self):
        mark_items_received(self.request, {self.item.pk: 2}, self.user)
        result = mark_refund_pending(self.request)
        self.assertEqual(result.status, ReturnRequest.STATUS_REFUND_PENDING)
        self.item.refresh_from_db()
        self.assertEqual(self.item.refund_amount, 50000 * 2)   # بدون تخفیف کوپن در این سفارش

    def test_partial_item_return_does_not_refund_shipping(self):
        mark_items_received(self.request, {self.item.pk: 1}, self.user)   # فقط ۱ از ۳ واحد کل سفارش
        result = mark_refund_pending(self.request)
        self.assertFalse(result.shipping_refunded)
        self.assertEqual(result.shipping_refund_amount, 0)

    def test_full_order_return_refunds_shipping_exactly_once(self):
        """
        این سفارش (self.order) از setUp از قبل یک درخواست فعال (self.request، quantity=2) روی
        self.order_item دارد؛ برای شبیه‌سازی تمیزِ «مرجوعی کامل»، سفارش/قلمِ کاملاً جدایی
        می‌سازیم که هیچ درخواست رقیبی روی ظرفیتش نباشد.
        """
        order = self.make_order(self.user, shipping_cost=30000)
        product = self.make_product(self.category, name='کالای مرجوعی کامل', slug='full-return-product', erp_code='ERP-FULLRETURN-1')
        order_item = self.make_order_item(order, product, price=50000, quantity=3)

        full_request = create_return_request(
            order, self.user,
            [{'order_item': order_item, 'reason': self.reason, 'requested_quantity': 3}],
            refund_method=ReturnRequest.REFUND_WALLET,
        )
        approve_return_request(full_request, self.user)
        full_item = full_request.items.get()
        mark_items_received(full_request, {full_item.pk: 3}, self.user)
        result = mark_refund_pending(full_request)
        self.assertTrue(result.shipping_refunded)
        self.assertEqual(result.shipping_refund_amount, 30000)
        self.assertEqual(result.total_refund_amount, 50000 * 3 + 30000)

    def test_wrong_state_raises(self):
        with self.assertRaises(InvalidReturnStateError):
            mark_refund_pending(self.request)   # هنوز APPROVED، نه ITEM_RECEIVED


class RejectReturnRequestTests(ServiceTestBase):
    def test_reject_from_each_rejectable_status(self):
        for status_setter in (
            lambda req: req,
            lambda req: approve_return_request(req, self.user),
            lambda req: mark_items_received(approve_return_request(req, self.user), {req.items.get().pk: 1}, self.user),
        ):
            with self.subTest():
                request = self.create_request(quantity=1)
                current = status_setter(request)
                rejected = reject_return_request(current, 'دلیل تست', self.user)
                self.assertEqual(rejected.status, ReturnRequest.STATUS_REJECTED)
                self.assertEqual(rejected.rejection_reason, 'دلیل تست')

    def test_reject_from_refund_pending(self):
        request = self.create_request(quantity=1)
        approve_return_request(request, self.user)
        item = request.items.get()
        mark_items_received(request, {item.pk: 1}, self.user)
        mark_refund_pending(request)
        rejected = reject_return_request(request, 'خطای بازرسی', self.user)
        self.assertEqual(rejected.status, ReturnRequest.STATUS_REJECTED)

    def test_cannot_reject_completed(self):
        request = self.create_request(quantity=1)
        approve_return_request(request, self.user)
        item = request.items.get()
        mark_items_received(request, {item.pk: 1}, self.user)
        mark_refund_pending(request)
        complete_refund(request, self.user)
        with self.assertRaises(InvalidReturnStateError):
            reject_return_request(request, 'خیلی دیر', self.user)

    def test_empty_reason_raises(self):
        request = self.create_request(quantity=1)
        with self.assertRaises(ValueError):
            reject_return_request(request, '   ', self.user)

    def test_rejecting_releases_returnable_capacity(self):
        request = self.create_request(quantity=self.order_item.quantity)
        self.assertEqual(get_returnable_quantity(self.order_item), 0)
        reject_return_request(request, 'منصرف شدم', self.user)
        self.assertEqual(get_returnable_quantity(self.order_item), self.order_item.quantity)

    def test_fires_signal_with_reason(self):
        request = self.create_request(quantity=1)
        received = []
        handler = lambda sender, return_request, reason, **kw: received.append((return_request.pk, reason))
        return_rejected.connect(handler, weak=False)
        self.addCleanup(return_rejected.disconnect, handler)
        with self.captureOnCommitCallbacks(execute=True):
            reject_return_request(request, 'دلیل امتحانی', self.user)
        self.assertEqual(received, [(request.pk, 'دلیل امتحانی')])


class CompleteRefundTests(ServiceTestBase):
    def _to_refund_pending(self, quantity=2, approved=2):
        request = self.create_request(quantity=quantity)
        approve_return_request(request, self.user)
        item = request.items.get()
        mark_items_received(request, {item.pk: approved}, self.user)
        return mark_refund_pending(request)

    def test_wallet_method_credits_wallet_and_links_transaction(self):
        request = self._to_refund_pending()
        expected_amount = request.total_refund_amount
        result = complete_refund(request, self.user)

        self.assertEqual(result.status, ReturnRequest.STATUS_COMPLETED)
        self.assertIsNotNone(result.wallet_transaction_id)
        txn = result.wallet_transaction
        self.assertEqual(txn.kind, WalletTransaction.KIND_REFUND)
        self.assertEqual(txn.amount, expected_amount)
        self.assertEqual(txn.reference_type, 'return_request')
        self.assertEqual(txn.reference_id, request.pk)

        wallet = Wallet.objects.get(user=self.user)
        self.assertEqual(wallet.balance, expected_amount)

    def test_bank_method_completes_without_touching_wallet(self):
        account = UserBankAccount.objects.create(
            user=self.user, account_holder_first_name='علی', account_holder_last_name='رضایی', card_number='6037991234567890',
        )
        request = create_return_request(
            self.order, self.user,
            [{'order_item': self.order_item, 'reason': self.reason, 'requested_quantity': 2}],
            refund_method=ReturnRequest.REFUND_BANK, bank_account=account,
        )
        approve_return_request(request, self.user)
        item = request.items.get()
        mark_items_received(request, {item.pk: 2}, self.user)
        mark_refund_pending(request)

        result = complete_refund(request, self.user)
        self.assertEqual(result.status, ReturnRequest.STATUS_COMPLETED)
        self.assertIsNone(result.wallet_transaction_id)
        self.assertFalse(Wallet.objects.filter(user=self.user).exists())

    def test_wrong_state_raises(self):
        request = self.create_request(quantity=1)
        with self.assertRaises(InvalidReturnStateError):
            complete_refund(request, self.user)

    def test_fires_signal(self):
        request = self._to_refund_pending()
        received = []
        handler = lambda sender, return_request, **kw: received.append(return_request.pk)
        return_refund_completed.connect(handler, weak=False)
        self.addCleanup(return_refund_completed.disconnect, handler)
        with self.captureOnCommitCallbacks(execute=True):
            complete_refund(request, self.user)
        self.assertEqual(received, [request.pk])

    def test_financial_snapshot_stays_frozen_after_completion(self):
        """
        Immutability: بعد از تکمیل، حتی اگر چیزی در سفارش (فرضاً) عوض شود، refund_amount قبلاً
        محاسبه‌شده نباید دوباره حساب یا بازنویسی شود - چون هیچ مسیری در complete_refund دوباره
        calculate_item_refund_amount را صدا نمی‌زند.
        """
        request = self._to_refund_pending()
        item = request.items.get()
        frozen_amount = item.refund_amount

        self.order.order_discount = 999999   # تغییر فرضی بعد از واقعیت - نباید اثری داشته باشد
        self.order.save(update_fields=['order_discount'])

        complete_refund(request, self.user)
        item.refresh_from_db()
        self.assertEqual(item.refund_amount, frozen_amount)


class ConcurrentCreateReturnRequestTests(ReturnsTestMixin, TransactionTestCase):
    """
    دو درخواست هم‌زمان برای مرجوعِ ظرفیتی که فقط برای یکی کافی است؛ قفل select_for_update روی
    ردیف OrderItem باید تضمین کند دقیقاً یکی موفق شود، نه هر دو (Overselling ظرفیت مرجوعی).
    """
    serialized_rollback = True

    def setUp(self):
        self.user = self.make_user('09140005001')
        self.category = self.make_category()
        self.order = self.make_order(self.user)
        self.product = self.make_product(self.category)
        self.order_item = self.make_order_item(self.order, self.product, price=10000, quantity=1)
        self.reason = self.make_reason()

    def test_only_one_of_two_concurrent_requests_succeeds(self):
        barrier = threading.Barrier(2)
        results = []

        def attempt():
            try:
                barrier.wait(timeout=30)
                request = create_return_request(
                    self.order, self.user,
                    [{'order_item': self.order_item, 'reason': self.reason, 'requested_quantity': 1}],
                    refund_method=ReturnRequest.REFUND_WALLET,
                )
                results.append(('ok', request.pk))
            except InsufficientReturnableQuantityError as exc:
                results.append(('rejected', str(exc)))
            except BaseException as exc:   # noqa: BLE001
                results.append(('error', repr(exc)))
            finally:
                connection.close()

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        outcomes = [r[0] for r in results]
        self.assertEqual(outcomes.count('ok'), 1, results)
        self.assertEqual(outcomes.count('rejected'), 1, results)
        self.assertEqual(ReturnRequest.objects.filter(order=self.order).count(), 1)
        self.assertEqual(get_returnable_quantity(self.order_item), 0)
