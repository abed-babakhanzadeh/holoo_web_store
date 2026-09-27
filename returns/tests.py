"""
تست‌های اولیه‌ی مدل‌های مرجوعی کالا (Phase 1 - بخش اول: فقط ساختار داده و ماشین وضعیت).
سرویس‌های واقعی تغییر وضعیت/محاسبه‌ی ریفاند در بخش دوم می‌آیند و تست‌های رفتاری آن‌ها هم آن‌جا.
این‌جا فقط: قیدهای دیتابیسی (CheckConstraint ها) و ولیدیشن سطح مدل درست تعریف شده باشند.
"""

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase

from accounts.models import CustomUser
from orders.models import Order, OrderItem
from products.models import Category, Product
from returns.models import ReturnAttachment, ReturnabilityRule, ReturnItem, ReturnReason, ReturnRequest


class ReturnsTestMixin:
    def make_user(self, phone='09140001001'):
        return CustomUser.objects.create_user(phone_number=phone)

    def make_category(self, name='دسته تست', slug='returns-test-cat', parent=None):
        return Category.objects.create(name=name, slug=slug, parent=parent)

    def make_product(self, category, name='کالای تست', slug='returns-test-product', erp_code='ERP-RETURNS-1'):
        return Product.objects.create(name=name, slug=slug, erp_code=erp_code, category=category, price=100000, stock=10)

    def make_order(self, user, **overrides):
        data = dict(
            user=user, first_name='علی', last_name='رضایی', phone=user.phone_number,
            address='تهران', payment_method='cash', shipping_cost=0, total_price=200000, status='delivered',
        )
        data.update(overrides)
        return Order.objects.create(**data)

    def make_order_item(self, order, product, quantity=2, price=100000):
        return OrderItem.objects.create(order=order, product=product, price=price, quantity=quantity)

    def make_reason(self, **overrides):
        data = dict(title='مغایرت با تصویر')
        data.update(overrides)
        return ReturnReason.objects.create(**data)

    def make_return_request(self, order, user, **overrides):
        data = dict(order=order, user=user, refund_method=ReturnRequest.REFUND_WALLET)
        data.update(overrides)
        return ReturnRequest.objects.create(**data)


class ReturnReasonTests(ReturnsTestMixin, TestCase):
    def test_str_and_default_ordering(self):
        first = self.make_reason(title='ب', order=2)
        second = self.make_reason(title='آ', order=1)
        self.assertEqual(str(second), 'آ')
        self.assertEqual(list(ReturnReason.objects.all()), [second, first])

    def test_default_shipping_payer_is_customer(self):
        reason = self.make_reason()
        self.assertEqual(reason.shipping_cost_payer, ReturnReason.PAYER_CUSTOMER)


class ReturnabilityRuleTests(ReturnsTestMixin, TestCase):
    def setUp(self):
        self.parent_category = self.make_category('پدر', 'returns-parent-cat')
        self.child_category = self.make_category('فرزند', 'returns-child-cat', parent=self.parent_category)
        self.other_category = self.make_category('نامرتبط', 'returns-other-cat')
        self.blocked_product = self.make_product(self.child_category, name='کالای مسدود', slug='blocked-product', erp_code='ERP-BLOCKED-1')
        self.free_product = self.make_product(self.other_category, name='کالای آزاد', slug='free-product', erp_code='ERP-FREE-1')

    def test_no_rule_means_returnable(self):
        self.assertIsNone(ReturnabilityRule.find_blocking_rule(self.free_product))

    def test_product_scope_blocks_only_that_product(self):
        rule = ReturnabilityRule.objects.create(scope=ReturnabilityRule.SCOPE_PRODUCT, reason_text='بهداشتی')
        rule.products.add(self.blocked_product)
        self.assertEqual(ReturnabilityRule.find_blocking_rule(self.blocked_product), rule)
        self.assertIsNone(ReturnabilityRule.find_blocking_rule(self.free_product))

    def test_category_scope_blocks_all_descendants(self):
        """ قاعده روی دسته‌ی پدر تعریف شده؛ محصولی در زیردسته‌ی فرزند هم باید مسدود شود """
        rule = ReturnabilityRule.objects.create(scope=ReturnabilityRule.SCOPE_CATEGORY, reason_text='بهداشتی')
        rule.categories.add(self.parent_category)
        self.assertEqual(ReturnabilityRule.find_blocking_rule(self.blocked_product), rule)
        self.assertIsNone(ReturnabilityRule.find_blocking_rule(self.free_product))

    def test_inactive_rule_does_not_block(self):
        rule = ReturnabilityRule.objects.create(scope=ReturnabilityRule.SCOPE_PRODUCT, reason_text='بهداشتی', is_active=False)
        rule.products.add(self.blocked_product)
        self.assertIsNone(ReturnabilityRule.find_blocking_rule(self.blocked_product))

    def test_product_scope_rule_wins_deterministically_over_category_scope(self):
        """
        اگر هم برای دسته‌ی والد قاعده‌ی مسدودکننده باشد هم برای خودِ محصول یک قاعده‌ی مستقیم،
        قاعده‌ی سطح محصول (خاص‌تر) باید برگردانده شود - نه هر کدام که تصادفاً زودتر در نتیجه‌ی
        دیتابیس بیاید. ترتیب ایجاد این‌جا عمداً برعکس (دسته اول، محصول دوم) است تا معلوم شود
        اولویت واقعاً از روی scope تعیین می‌شود، نه از روی ترتیب pk/insertion.
        """
        category_rule = ReturnabilityRule.objects.create(
            scope=ReturnabilityRule.SCOPE_CATEGORY, reason_text='کل دسته بهداشتی است',
        )
        category_rule.categories.add(self.parent_category)
        product_rule = ReturnabilityRule.objects.create(
            scope=ReturnabilityRule.SCOPE_PRODUCT, reason_text='این محصول به‌طور خاص مسدود است',
        )
        product_rule.products.add(self.blocked_product)

        self.assertEqual(ReturnabilityRule.find_blocking_rule(self.blocked_product), product_rule)


class ReturnRequestConstraintTests(ReturnsTestMixin, TestCase):
    def setUp(self):
        self.user = self.make_user()
        self.order = self.make_order(self.user)

    def test_plain_pending_wallet_request_is_valid(self):
        request = self.make_return_request(self.order, self.user)
        self.assertEqual(request.status, ReturnRequest.STATUS_PENDING)

    def test_rejected_without_reason_is_rejected_by_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_return_request(self.order, self.user, status=ReturnRequest.STATUS_REJECTED)

    def test_rejected_with_reason_is_accepted(self):
        request = self.make_return_request(
            self.order, self.user, status=ReturnRequest.STATUS_REJECTED, rejection_reason='کالا مصرف‌شده بود',
        )
        self.assertEqual(request.status, ReturnRequest.STATUS_REJECTED)

    def test_bank_refund_without_any_account_snapshot_is_rejected_by_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_return_request(self.order, self.user, refund_method=ReturnRequest.REFUND_BANK)

    def test_bank_refund_with_card_snapshot_is_accepted(self):
        request = self.make_return_request(
            self.order, self.user, refund_method=ReturnRequest.REFUND_BANK, bank_card_snapshot='6037991234567890',
        )
        self.assertEqual(request.bank_card_snapshot, '6037991234567890')

    def test_completed_status_without_timestamp_is_rejected_by_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_return_request(self.order, self.user, status=ReturnRequest.STATUS_COMPLETED)

    def test_completed_status_with_timestamp_is_accepted(self):
        from django.utils import timezone
        request = self.make_return_request(
            self.order, self.user, status=ReturnRequest.STATUS_COMPLETED, completed_at=timezone.now(),
        )
        self.assertEqual(request.status, ReturnRequest.STATUS_COMPLETED)

    def test_str_contains_order_id(self):
        request = self.make_return_request(self.order, self.user)
        self.assertIn(str(self.order.id), str(request))


class ReturnItemConstraintTests(ReturnsTestMixin, TestCase):
    def setUp(self):
        self.user = self.make_user()
        self.order = self.make_order(self.user)
        self.category = self.make_category()
        self.product = self.make_product(self.category)
        self.order_item = self.make_order_item(self.order, self.product, quantity=3)
        self.reason = self.make_reason()
        self.request = self.make_return_request(self.order, self.user)

    def make_return_item(self, **overrides):
        data = dict(return_request=self.request, order_item=self.order_item, reason=self.reason, requested_quantity=1)
        data.update(overrides)
        return ReturnItem.objects.create(**data)

    def test_requested_quantity_must_be_positive(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_return_item(requested_quantity=0)

    def test_approved_quantity_can_be_null(self):
        item = self.make_return_item(requested_quantity=2)
        self.assertIsNone(item.approved_quantity)

    def test_approved_quantity_cannot_exceed_requested(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_return_item(requested_quantity=1, approved_quantity=2)

    def test_approved_quantity_equal_to_requested_is_accepted(self):
        item = self.make_return_item(requested_quantity=2, approved_quantity=2)
        self.assertEqual(item.approved_quantity, 2)

    def test_approved_quantity_less_than_requested_is_accepted(self):
        item = self.make_return_item(requested_quantity=3, approved_quantity=1)
        self.assertEqual(item.approved_quantity, 1)


class ReturnAttachmentTests(ReturnsTestMixin, TestCase):
    def setUp(self):
        self.user = self.make_user()
        self.order = self.make_order(self.user)
        self.category = self.make_category()
        self.product = self.make_product(self.category)
        self.order_item = self.make_order_item(self.order, self.product)
        self.reason = self.make_reason()
        self.request = self.make_return_request(self.order, self.user)
        self.item = ReturnItem.objects.create(
            return_request=self.request, order_item=self.order_item, reason=self.reason, requested_quantity=1,
        )

    def make_attachment(self, content=b'x', filename='photo.jpg', attachment_type=ReturnAttachment.IMAGE, do_clean=True):
        attachment = ReturnAttachment(
            return_item=self.item, file=SimpleUploadedFile(filename, content), attachment_type=attachment_type,
            original_filename=filename,
        )
        if do_clean:
            attachment.clean()
        attachment.save()
        return attachment

    def test_remaining_slots_starts_at_max(self):
        self.assertEqual(ReturnAttachment.remaining_slots(self.item), ReturnAttachment.MAX_PER_ITEM)

    def test_remaining_slots_decreases_after_upload(self):
        self.make_attachment()
        self.assertEqual(ReturnAttachment.remaining_slots(self.item), ReturnAttachment.MAX_PER_ITEM - 1)

    def test_sixth_attachment_is_rejected_by_clean(self):
        for i in range(ReturnAttachment.MAX_PER_ITEM):
            self.make_attachment(filename=f'photo{i}.jpg')
        self.assertEqual(ReturnAttachment.remaining_slots(self.item), 0)
        with self.assertRaises(ValidationError) as ctx:
            self.make_attachment(filename='photo-extra.jpg')
        self.assertIn('return_item', ctx.exception.message_dict)

    def test_oversized_image_is_rejected_by_clean(self):
        oversized = b'0' * (ReturnAttachment.MAX_IMAGE_SIZE_MB * 1024 * 1024 + 1)
        with self.assertRaises(ValidationError) as ctx:
            self.make_attachment(content=oversized, filename='huge.jpg')
        self.assertIn('file', ctx.exception.message_dict)

    def test_video_uses_its_own_larger_size_cap(self):
        just_under_video_cap = b'0' * (ReturnAttachment.MAX_IMAGE_SIZE_MB * 1024 * 1024 + 1)
        attachment = self.make_attachment(content=just_under_video_cap, filename='clip.mp4', attachment_type=ReturnAttachment.VIDEO)
        self.assertEqual(attachment.attachment_type, ReturnAttachment.VIDEO)

    def test_str_falls_back_to_pk_without_filename(self):
        attachment = self.make_attachment(filename='photo.jpg')
        attachment.original_filename = ''
        attachment.save()
        self.assertIn(str(attachment.pk), str(attachment))

    def test_cap_is_enforced_by_save_even_without_an_explicit_clean_call(self):
        """
        اگر فراخوان‌کننده مستقیم .objects.create() بزند (بدون فرم، بدون صدا زدن clean() دستی)
        سقف باید همچنان رد شود - save() خودش clean() را صدا می‌زند.
        """
        for i in range(ReturnAttachment.MAX_PER_ITEM):
            ReturnAttachment.objects.create(
                return_item=self.item, file=SimpleUploadedFile(f'bare{i}.jpg', b'x'),
                attachment_type=ReturnAttachment.IMAGE,
            )
        with self.assertRaises(ValidationError):
            ReturnAttachment.objects.create(
                return_item=self.item, file=SimpleUploadedFile('bare-extra.jpg', b'x'),
                attachment_type=ReturnAttachment.IMAGE,
            )
        self.assertEqual(ReturnAttachment.objects.filter(return_item=self.item).count(), ReturnAttachment.MAX_PER_ITEM)

    def test_cap_is_per_return_item_not_per_return_request(self):
        """ دو قلم مرجوعیِ متفاوت در همان درخواست، هرکدام سهمیه‌ی ۵تایی مستقل خودشان را دارند """
        second_order_item = self.make_order_item(self.order, self.product, quantity=1)
        second_item = ReturnItem.objects.create(
            return_request=self.request, order_item=second_order_item, reason=self.reason, requested_quantity=1,
        )
        for i in range(ReturnAttachment.MAX_PER_ITEM):
            self.make_attachment(filename=f'first-{i}.jpg')
        self.assertEqual(ReturnAttachment.remaining_slots(self.item), 0)
        self.assertEqual(ReturnAttachment.remaining_slots(second_item), ReturnAttachment.MAX_PER_ITEM)

        second_attachment = ReturnAttachment.objects.create(
            return_item=second_item, file=SimpleUploadedFile('second.jpg', b'x'), attachment_type=ReturnAttachment.IMAGE,
        )
        self.assertEqual(second_attachment.return_item_id, second_item.pk)
