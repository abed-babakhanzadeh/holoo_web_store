"""
تنها لایه‌ی مجاز برای تغییر وضعیت ReturnRequest/ReturnItem.

هیچ کد دیگری (ادمین، ویو، شل) نباید مستقیم status را عوض کند یا refund_amount بنویسد - همه‌ی
مسیرها باید از این توابع رد شوند تا قفل ردیفی، اتمیک‌بودن و ترتیب گذارهای وضعیت (state machine)
در یک‌جا تضمین شوند. الگوی قفل/اتمیک/سیگنال دقیقاً هم‌شکل wallet/services.py است؛ این اپ هرگز
notify()/notify_admin() را مستقیم صدا نمی‌زند - فقط سیگنال‌های returns/signals.py را شلیک
می‌کند (نگاه کنید notifications/receivers.py برای لایه‌ی واکنش).
"""

from django.db import transaction as db_transaction
from django.utils import timezone

from orders.models import OrderItem
from products.models import SiteSettings
from wallet import services as wallet_services
from wallet.models import Wallet, WalletTransaction

from .deadline import calculate_return_deadline
from .models import ReturnAttachment, ReturnItem, ReturnRequest
from .refund_calculator import calculate_item_refund_amount, calculate_shipping_refund, get_returnable_quantity
from .signals import (
    return_approved, return_item_received, return_refund_completed, return_rejected, return_requested,
)

# رد از هر یک از این ۴ وضعیت مجاز است؛ COMPLETED هرگز رد نمی‌شود (پول قبلاً واقعاً منتقل شده)
_REJECTABLE_STATUSES = (
    ReturnRequest.STATUS_PENDING, ReturnRequest.STATUS_APPROVED,
    ReturnRequest.STATUS_ITEM_RECEIVED, ReturnRequest.STATUS_REFUND_PENDING,
)


class InvalidReturnStateError(Exception):
    """ این درخواست در وضعیتی نیست که این گذار روی آن مجاز باشد. """


class InsufficientReturnableQuantityError(Exception):
    """ تعداد درخواستی از ظرفیت باقیمانده‌ی قابل‌مرجوعِ این قلم بیشتر است. """


class OrderNotDeliveredError(Exception):
    """ سفارش هنوز تحویل داده نشده (یا زمان تحویل ثبت نشده) - نمی‌توان برایش درخواست مرجوعی ثبت کرد. """


class ReturnWindowExpiredError(Exception):
    """ مهلت مرجوعی این سفارش (بر مبنای SiteSettings.return_period_days/unit) گذشته است. """


def create_return_request(order, user, items, *, refund_method, bank_account=None):
    """
    ثبت یک درخواست مرجوعی با یک یا چند قلم.

    items: فهرستی از دیکشنری {'order_item': OrderItem, 'reason': ReturnReason,
           'requested_quantity': int, 'description': str (اختیاری),
           'attachments': فهرست دیکشنری {'file': File-like, 'attachment_type': 'image'/'video',
                          'original_filename': str} (اختیاری، Part C.3)}.
    bank_account: فقط برای refund_method=bank؛ اطلاعاتش همین‌جا اسنپ‌شات می‌شود (اگر کاربر
    بعداً حساب را حذف/ویرایش کند، این درخواست دست نمی‌خورد - دقیقاً هم‌دلیل
    WithdrawalRequest.card_number_snapshot).

    قفل: ردیف‌های خودِ OrderItem های درگیر (نه یک ردیف مجازی جدا) قفل می‌شوند - هم‌الگوی
    accounts/address.py:_lock_user_rows (قفل روی «مالک»، نه روی چیزی که هنوز وجود ندارد).
    دو درخواست هم‌زمان برای همان OrderItem پشت این قفل صف می‌شوند؛ دومی بعد از commit اولی
    ظرفیتِ به‌روزشده (get_returnable_quantity) را می‌بیند، نه نسخه‌ی قدیمی را.
    """
    items = list(items)
    if not items:
        raise ValueError('حداقل یک قلم برای ثبت درخواست مرجوعی لازم است.')
    if order.user_id != user.id:
        raise ValueError('این سفارش متعلق به این کاربر نیست.')
    if refund_method == ReturnRequest.REFUND_BANK and bank_account is None:
        raise ValueError('برای بازپرداخت به حساب بانکی، انتخاب یک حساب الزامی است.')

    # گارد مهلت مرجوعی: قانون بحرانی کسب‌وکار، پس فقط در فرم/فرانت‌اند چک نمی‌شود - همین‌جا و
    # قبل از هر قفل/نوشتنی رد می‌شود (نگاه کنید returns/deadline.py برای منطق شمارش روز کاری).
    if order.status != 'delivered' or not order.delivered_at:
        raise OrderNotDeliveredError('این سفارش هنوز تحویل داده نشده است؛ ثبت درخواست مرجوعی ممکن نیست.')

    settings_obj = SiteSettings.cached()
    deadline = calculate_return_deadline(order.delivered_at, settings_obj.return_period_days, settings_obj.return_period_unit)
    if timezone.now() > deadline:
        raise ReturnWindowExpiredError(
            f'مهلت {settings_obj.return_period_days} روزه‌ی مرجوعی این سفارش در تاریخ '
            f'{deadline:%Y-%m-%d %H:%M} به پایان رسیده است.'
        )

    order_item_ids = sorted({entry['order_item'].pk for entry in items})

    with db_transaction.atomic():
        locked_order_items = {
            oi.pk: oi for oi in OrderItem.objects.select_for_update().filter(pk__in=order_item_ids)
        }

        return_request = ReturnRequest(order=order, user=user, refund_method=refund_method)
        if refund_method == ReturnRequest.REFUND_BANK:
            return_request.bank_account = bank_account
            return_request.bank_account_holder_snapshot = bank_account.account_holder_full_name
            return_request.bank_card_snapshot = bank_account.card_number
            return_request.bank_iban_snapshot = bank_account.iban
        return_request.save()

        return_items = []
        for entry in items:
            order_item = locked_order_items.get(entry['order_item'].pk)
            if order_item is None or order_item.order_id != order.id:
                raise ValueError('یکی از اقلام درخواستی متعلق به این سفارش نیست.')

            requested_quantity = int(entry['requested_quantity'])
            if requested_quantity <= 0:
                raise ValueError('تعداد درخواستی باید بزرگ‌تر از صفر باشد.')

            returnable = get_returnable_quantity(order_item)
            if requested_quantity > returnable:
                raise InsufficientReturnableQuantityError(
                    f'تعداد درخواستی ({requested_quantity}) برای قلم #{order_item.pk} از ظرفیت '
                    f'باقیمانده‌ی قابل‌مرجوع ({returnable}) بیشتر است.'
                )
            # تک‌تک ذخیره می‌شوند (نه bulk_create) چون بلافاصله به pk واقعی نیاز داریم تا
            # مدارک همین قلم (پایین) به رکورد درست وصل شوند؛ bulk_create روی همه‌ی بک‌اندها
            # pk را بعد از درج برنمی‌گرداند.
            return_item = ReturnItem.objects.create(
                return_request=return_request, order_item=order_item, reason=entry['reason'],
                requested_quantity=requested_quantity, description=entry.get('description', ''),
            )
            for attachment in entry.get('attachments', []):
                ReturnAttachment.objects.create(
                    return_item=return_item, file=attachment['file'],
                    attachment_type=attachment['attachment_type'],
                    original_filename=attachment.get('original_filename', ''),
                )
            return_items.append(return_item)

        db_transaction.on_commit(
            lambda: return_requested.send_robust(sender=ReturnRequest, return_request=return_request),
        )
    return return_request


def approve_return_request(return_request, by):
    """ PENDING -> APPROVED. فقط تغییر وضعیت؛ هیچ مبلغی این‌جا محاسبه نمی‌شود. """
    with db_transaction.atomic():
        locked = ReturnRequest.objects.select_for_update().get(pk=return_request.pk)
        if locked.status != ReturnRequest.STATUS_PENDING:
            raise InvalidReturnStateError('این درخواست در وضعیت «در انتظار بررسی» نیست.')

        locked.status = ReturnRequest.STATUS_APPROVED
        locked.decided_at = timezone.now()
        locked.decided_by = by
        locked.save(update_fields=['status', 'decided_at', 'decided_by'])

        db_transaction.on_commit(
            lambda: return_approved.send_robust(sender=ReturnRequest, return_request=locked),
        )
    return locked


def mark_items_received(return_request, approved_quantities, by):
    """
    APPROVED -> ITEM_RECEIVED. approved_quantities: دیکشنری {return_item_id: تعداد تأییدشده} که
    باید دقیقاً همه‌ی اقلام همین درخواست را (نه کم‌تر، نه قلمِ نامربوط) پوشش دهد.
    approved_quantity هر قلم باید بین ۰ و requested_quantity همان قلم باشد (قید هم‌نام هم در
    سطح دیتابیس هست؛ این‌جا برای خطای واضح‌تر پیش از رسیدن به IntegrityError چک می‌شود).
    """
    with db_transaction.atomic():
        locked = ReturnRequest.objects.select_for_update().get(pk=return_request.pk)
        if locked.status != ReturnRequest.STATUS_APPROVED:
            raise InvalidReturnStateError('این درخواست ابتدا باید تأیید شود (وضعیت APPROVED).')

        items = list(locked.items.select_for_update())
        if {item.pk for item in items} != set(approved_quantities.keys()):
            raise ValueError('باید تعداد تأییدشده برای دقیقاً همه‌ی اقلام این درخواست مشخص شود.')

        for item in items:
            approved_quantity = int(approved_quantities[item.pk])
            if not (0 <= approved_quantity <= item.requested_quantity):
                raise ValueError(
                    f'تعداد تأییدشده‌ی قلم #{item.pk} باید بین ۰ و {item.requested_quantity} باشد.'
                )
            item.approved_quantity = approved_quantity
            item.save(update_fields=['approved_quantity'])

        locked.status = ReturnRequest.STATUS_ITEM_RECEIVED
        locked.item_received_at = timezone.now()
        locked.received_by = by
        locked.save(update_fields=['status', 'item_received_at', 'received_by'])

        db_transaction.on_commit(
            lambda: return_item_received.send_robust(sender=ReturnRequest, return_request=locked),
        )
    return locked


def mark_refund_pending(return_request):
    """
    ITEM_RECEIVED -> REFUND_PENDING؛ لحظه‌ی «قطعی‌شدن» مبالغ مالی (Immutability):
    refund_amount هر قلم و shipping_refund_amount/shipping_refunded خودِ درخواست، همین‌جا
    یک‌بار برای همیشه محاسبه و در دیتابیس نوشته می‌شوند - بعد از این دیگر هیچ‌کدام دوباره
    محاسبه/بازنویسی نمی‌شوند (نه در complete_refund، نه در هیچ نمایشی دیگر).
    """
    with db_transaction.atomic():
        locked = ReturnRequest.objects.select_for_update().select_related('order').get(pk=return_request.pk)
        if locked.status != ReturnRequest.STATUS_ITEM_RECEIVED:
            raise InvalidReturnStateError('این درخواست ابتدا باید در وضعیت «کالا دریافت شد» باشد.')

        items = list(locked.items.select_related('order_item__order'))
        for item in items:
            item.refund_amount = calculate_item_refund_amount(item)
            item.save(update_fields=['refund_amount'])

        # وضعیت را همین‌جا (پیش از محاسبه‌ی کرایه) REFUND_PENDING می‌کنیم و ذخیره می‌کنیم؛
        # is_full_order_return/calculate_shipping_refund فقط مرجوعی‌های REFUND_PENDING/COMPLETED
        # را می‌شمارند، پس اگر همین درخواست خودش سفارش را کامل می‌کند باید از هم‌اکنون در آن
        # جمع دیده شود - وگرنه هیچ‌وقت واجد شرایط استرداد کرایه شمرده نمی‌شد.
        locked.status = ReturnRequest.STATUS_REFUND_PENDING
        locked.save(update_fields=['status'])

        shipping_amount = calculate_shipping_refund(locked.order, exclude_return_request=locked)
        if shipping_amount > 0:
            locked.shipping_refunded = True
            locked.shipping_refund_amount = shipping_amount
            locked.save(update_fields=['shipping_refunded', 'shipping_refund_amount'])
    return locked


def reject_return_request(return_request, reason, by):
    """ از هر یک از ۴ وضعیت فعالِ پیش از COMPLETED مجاز است؛ دلیل رد الزامی است. """
    if not (reason or '').strip():
        raise ValueError('دلیل رد الزامی است.')

    with db_transaction.atomic():
        locked = ReturnRequest.objects.select_for_update().get(pk=return_request.pk)
        if locked.status not in _REJECTABLE_STATUSES:
            raise InvalidReturnStateError('این درخواست دیگر قابل رد شدن نیست.')

        locked.status = ReturnRequest.STATUS_REJECTED
        locked.rejection_reason = reason
        locked.decided_at = timezone.now()
        locked.decided_by = by
        locked.save(update_fields=['status', 'rejection_reason', 'decided_at', 'decided_by'])

        db_transaction.on_commit(
            lambda: return_rejected.send_robust(sender=ReturnRequest, return_request=locked, reason=reason),
        )
    return locked


def complete_refund(return_request, by):
    """
    REFUND_PENDING -> COMPLETED. اگر refund_method=wallet باشد، مبلغ (total_refund_amount -
    همان اسنپ‌شات‌های قطعیِ mark_refund_pending) با wallet.services.credit_wallet به کیف‌پول
    کاربر واریز و wallet_transaction لینک می‌شود. اگر refund_method=bank باشد، فقط وضعیت
    تکمیل می‌شود - واریز واقعی کاملاً دستی توسط حسابداری انجام می‌شود و سایت هیچ سندی ثبت نمی‌کند.
    """
    with db_transaction.atomic():
        locked = ReturnRequest.objects.select_for_update().select_related('user', 'order').get(pk=return_request.pk)
        if locked.status != ReturnRequest.STATUS_REFUND_PENDING:
            raise InvalidReturnStateError('این درخواست ابتدا باید در صف بازپرداخت (REFUND_PENDING) باشد.')

        if locked.refund_method == ReturnRequest.REFUND_WALLET:
            amount = locked.total_refund_amount
            if amount <= 0:
                raise ValueError('مبلغ بازپرداخت باید بزرگ‌تر از صفر باشد.')
            wallet, _ = Wallet.objects.get_or_create(user=locked.user)
            txn = wallet_services.credit_wallet(
                wallet, amount, WalletTransaction.KIND_REFUND,
                reference_type='return_request', reference_id=locked.pk,
                description=f'مرجوعی سفارش #{locked.order_id}', created_by=by,
            )
            locked.wallet_transaction = txn

        locked.status = ReturnRequest.STATUS_COMPLETED
        locked.completed_at = timezone.now()
        locked.completed_by = by
        locked.save(update_fields=['status', 'completed_at', 'completed_by', 'wallet_transaction'])

        db_transaction.on_commit(
            lambda: return_refund_completed.send_robust(sender=ReturnRequest, return_request=locked),
        )
    return locked
