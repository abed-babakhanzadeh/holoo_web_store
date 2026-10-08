"""
تنها نقطه‌ی اتصال اطلاع‌رسانی به رویدادهای پروژه.

payments و accounts دیگر نمی‌دانند پس از پرداخت یا تکمیل پروفایل پیامی می‌رود یا نه.
برای اضافه/کم کردن یک اطلاع‌رسانی، فقط همین فایل و templates_registry.py دست می‌خورند.
"""

import logging
import re

from django.core.cache import cache
from django.dispatch import receiver
from django.utils import timezone

from accounts.signals import (cheque_credit_approved, cheque_credit_rejected, cheque_credit_requested, profile_completed,
                              user_approved, user_registered, user_resubmitted_for_review)
from orders.signals import cheque_deadline_expired, cheque_reviewed, cheque_submitted, order_approved, order_placed
from payments.signals import payment_succeeded
from products.signals import contact_message_received, product_back_in_stock
from returns.signals import (
    return_approved, return_item_received, return_refund_completed, return_refund_queued, return_rejected,
    return_requested,
)

from .service import notify, notify_admin

logger = logging.getLogger(__name__)


@receiver(order_placed, dispatch_uid='notify_order_placed')
def on_order_placed(sender, order, **kwargs):
    if order.is_cheque:
        # سفارش چکی «در حال پردازش» نیست؛ مشتری باید بداند چک را ثبت کند (پیام خودش، نه پیام عمومیِ ثبت سفارش)
        _notify_cheque_order_placed(order)
        return
    notify(
        order.user.phone_number, 'order_placed_customer',
        name=order.user.first_name or '', order_id=order.id,
    )


@receiver(order_approved, dispatch_uid='notify_order_approved')
def on_order_approved(sender, order, **kwargs):
    # مدیر سفارش را تأیید کرد (فاکتور قطعی هم از همین لحظه صادر می‌شود)؛ مشتری باید بداند
    if order.user is not None:
        notify(order.user.phone_number, 'order_approved_customer',
               name=order.user.first_name or '', order_id=order.id)


@receiver(payment_succeeded, dispatch_uid='notify_payment_succeeded')
def on_payment_succeeded(sender, order, transaction, **kwargs):
    amount = f"{order.total_price:,.0f}"
    notify(
        order.user.phone_number, 'payment_succeeded_customer',
        name=order.user.first_name or '', amount=amount, ref_id=transaction.ref_id,
    )
    notify_admin(
        'payment_succeeded_admin',
        order_id=order.id, amount=amount, phone=order.user.phone_number,
    )


@receiver(user_registered, dispatch_uid='notify_user_registered')
def on_user_registered(sender, user, **kwargs):
    notify_admin('user_registered_admin', phone=user.phone_number)


@receiver(profile_completed, dispatch_uid='notify_profile_completed')
def on_profile_completed(sender, user, **kwargs):
    notify_admin(
        'profile_completed_admin',
        full_name=f"{user.first_name or ''} {user.last_name or ''}".strip(),
        phone=user.phone_number,
    )


@receiver(user_resubmitted_for_review, dispatch_uid='notify_user_resubmitted_for_review')
def on_user_resubmitted_for_review(sender, user, **kwargs):
    # قالب اختصاصی، نه profile_completed_admin: این پرونده قبلاً یک‌بار رد شده، مدیر باید
    # بداند با تکمیل اولیه‌ی یک مشتری تازه طرف نیست، بلکه با اصلاح یک پرونده‌ی ردشده
    notify_admin(
        'user_resubmitted_admin',
        full_name=f"{user.first_name or ''} {user.last_name or ''}".strip(),
        phone=user.phone_number,
    )


@receiver(user_approved, dispatch_uid='notify_user_approved')
def on_user_approved(sender, user, **kwargs):
    notify(user.phone_number, 'account_approved_customer', name=user.first_name or '')


@receiver(product_back_in_stock, dispatch_uid='notify_product_back_in_stock')
def on_product_back_in_stock(sender, product, **kwargs):
    from products.models import StockAlert

    alerts = list(
        StockAlert.objects.filter(product=product, status=StockAlert.STATUS_PENDING).select_related('user')
    )
    if not alerts:
        return

    # وضعیت را قبل از ارسال واقعی flip می‌کنیم (نه بعدش): notify() خودش صف/تلاش‌مجدد ارسال
    # را مدیریت می‌کند، پس همین‌جا «صف شد» به معنای انجام‌شده است — دقیقاً همان قراردادی که
    # بقیه‌ی پروژه هم دارد (مثل holoo_sync_alert_sent بلافاصله بعد از notify_admin).
    StockAlert.objects.filter(id__in=[a.id for a in alerts]).update(
        status=StockAlert.STATUS_NOTIFIED, notified_at=timezone.now(),
    )

    for alert in alerts:
        if alert.channel in (StockAlert.CHANNEL_EMAIL, StockAlert.CHANNEL_BOTH):
            notify(
                alert.email or alert.user.email, 'back_in_stock_email',
                backend='notifications.backends.email.EmailBackend',
                name=alert.user.first_name or '', product_name=product.name,
            )
        if alert.channel in (StockAlert.CHANNEL_SMS, StockAlert.CHANNEL_BOTH):
            notify(alert.user.phone_number, 'back_in_stock_sms', product_name=product.name)


def _sms_text(value, limit):
    """ متن کاربر را برای پیامک امن می‌کند: بدون خط جدید/کاراکتر کنترلی، لینک جایگزین می‌شود (تا فرم تماس کانالی
    برای رساندن لینک فیشینگ به پیامک مدیر نباشد) و طول محدود می‌شود """
    value = re.sub(r'(?:https?://|www\.)\S+', '[لینک]', value or '')
    value = re.sub(r'\s+', ' ', value).strip()
    return value if len(value) <= limit else value[:limit - 1].rstrip() + '…'


@receiver(contact_message_received, dispatch_uid='notify_contact_message_received')
def on_contact_message_received(sender, message, **kwargs):
    from products.models import SiteSettings

    context = {'name': _sms_text(message.name, 40), 'subject': _sms_text(message.subject, 60)}
    # گیرنده‌ها اول از مشخصات فروشگاه (حداکثر دو شماره‌ی مدیر، بدون تکرار)؛ اگر هیچ‌کدام پر نبود همان گیرنده‌ی
    # پیش‌فرض اعلان مدیر در تنظیمات سرور (notify_admin). ارسال به هر شماره مستقل است: خطا در یکی نباید
    # شماره‌ی دیگر را بی‌پیام بگذارد (notify هرگز استثنا نمی‌اندازد، ولی محافظ اضافه هم ضرری ندارد).
    recipients = SiteSettings.cached().store_admin_sms_recipients
    if not recipients:
        notify_admin('contact_message_admin', **context)
        return
    for recipient in recipients:
        try:
            notify(recipient, 'contact_message_admin', **context)
        except Exception:
            logger.exception('ارسال اعلان تماس با ما به %s ناموفق بود.', recipient)


# --- مرجوعی کالا: هر مرحله پیامی برای مشتری (و ثبت درخواست برای مدیر هم) ---
# ReturnRequest.user همیشه هست (FK محافظت‌شده)؛ نام و تلفن از خودِ کاربر درخواست خوانده می‌شود.

def _return_ctx(return_request):
    user = return_request.user
    return user.phone_number, {'name': user.first_name or '', 'order_id': return_request.order_id}


@receiver(return_requested, dispatch_uid='notify_return_requested')
def on_return_requested(sender, return_request, **kwargs):
    phone, ctx = _return_ctx(return_request)
    notify(phone, 'return_requested_customer', **ctx)
    notify_admin('return_requested_admin', order_id=return_request.order_id, phone=phone)


@receiver(return_approved, dispatch_uid='notify_return_approved')
def on_return_approved(sender, return_request, **kwargs):
    phone, ctx = _return_ctx(return_request)
    notify(phone, 'return_approved_customer', **ctx)


@receiver(return_item_received, dispatch_uid='notify_return_item_received')
def on_return_item_received(sender, return_request, **kwargs):
    phone, ctx = _return_ctx(return_request)
    notify(phone, 'return_item_received_customer', **ctx)


@receiver(return_refund_queued, dispatch_uid='notify_return_refund_queued')
def on_return_refund_queued(sender, return_request, **kwargs):
    phone, ctx = _return_ctx(return_request)
    notify(phone, 'return_refund_pending_customer', amount=f"{return_request.total_refund_amount:,.0f}", **ctx)


@receiver(return_rejected, dispatch_uid='notify_return_rejected')
def on_return_rejected(sender, return_request, reason='', **kwargs):
    phone, ctx = _return_ctx(return_request)
    notify(phone, 'return_rejected_customer', reason=_sms_text(reason, 120) or 'نامشخص', **ctx)


@receiver(return_refund_completed, dispatch_uid='notify_return_refund_completed')
def on_return_refund_completed(sender, return_request, **kwargs):
    from returns.models import ReturnRequest
    phone, ctx = _return_ctx(return_request)
    destination = 'کیف پول' if return_request.refund_method == ReturnRequest.REFUND_WALLET else 'حساب بانکی'
    notify(phone, 'return_refund_completed_customer', amount=f"{return_request.total_refund_amount:,.0f}",
           destination=destination, **ctx)


# --- چک (فاز E): ثبت سفارش چکی، ثبت/اصلاح چک (به مدیر)، تأیید/رد چک و لغو خودکار (به مشتری) ---
# رویدادها از سرویس‌های orders/cheques.py و orders/deadline.py (و در نتیجه ویوها، ادمین و تسک Beat) می‌آیند. ارسال از notify():
# ناهمگام (تسک Celery)، پس از commit، با کلیدهای قابل خاموش/روشن در پنل. فقط برای شماره‌ی موبایل معتبر؛ و با cooldown تا
# رد/تأییدِ پشت‌سرهم یا چند چکِ پیاپی، پیامک‌های تکراری نسازد.

CHEQUE_CUSTOMER_COOLDOWN = 60            # ثانیه، برای هر (نوع پیام، سفارش)
CHEQUE_ADMIN_COOLDOWN = 10 * 60


def _cooldown_ok(kind, order_id, seconds):
    """ True اگر در این بازه برای همین (نوع، سفارش) پیامی نرفته بود. Redis قطع ← ارسال می‌شود (اعلان گم نشود) """
    try:
        return bool(cache.add(f'notify:cheque:{kind}:{order_id}', 1, seconds))
    except Exception:  # noqa: BLE001
        return True


def _valid_phone(user):
    """ شماره‌ی موبایلِ نرمال‌شده‌ی کاربر یا None (بدون کاربر/شماره‌ی نامعتبر ← پیامکی نمی‌رود) """
    if user is None:
        return None
    try:
        from accounts.models import normalize_phone_number
        return normalize_phone_number(user.phone_number)
    except (ValueError, TypeError):
        logger.warning('شماره‌ی کاربر %s برای پیامک چک معتبر نیست.', getattr(user, 'pk', None))
        return None


def _cancel_note(order):
    return ' در غیر این صورت سفارش لغو می‌شود.' if order.cheque_deadline_at else ''


def _notify_store_admins(template_key, **context):
    """ مدیرها: شماره‌های مشخصات فروشگاه (تا دو شماره)؛ اگر خالی بود گیرنده‌ی پیش‌فرض اعلان مدیر """
    from products.models import SiteSettings
    recipients = SiteSettings.cached().store_admin_sms_recipients
    if not recipients:
        notify_admin(template_key, **context)
        return
    for recipient in recipients:
        try:
            notify(recipient, template_key, **context)
        except Exception:  # noqa: BLE001
            logger.exception('ارسال اعلان %s به %s ناموفق بود.', template_key, recipient)


def _notify_cheque_order_placed(order):
    from orders.deadline import deadline_note
    phone = _valid_phone(order.user)
    if not phone:
        return
    notify(phone, 'cheque_order_placed_customer', name=order.user.first_name or '', order_id=order.id,
           deadline_note=deadline_note(order), cancel_note=_cancel_note(order))


@receiver(cheque_submitted, dispatch_uid='notify_cheque_submitted')
def on_cheque_submitted(sender, order, cheque, resubmitted=False, **kwargs):
    if not _cooldown_ok('submitted', order.id, CHEQUE_ADMIN_COOLDOWN):
        return
    _notify_store_admins('cheque_registered_admin', order_id=order.id, phone=order.user.phone_number if order.user else '',
                         action='اصلاح و دوباره ارسال شد' if resubmitted else 'ثبت شد')


@receiver(cheque_reviewed, dispatch_uid='notify_cheque_reviewed')
def on_cheque_reviewed(sender, order, cheque, status, reason='', **kwargs):
    from orders.deadline import deadline_note
    from orders.models import ChequePayment
    phone = _valid_phone(order.user)
    if not phone:
        return
    name = order.user.first_name or ''
    if status == ChequePayment.STATUS_REJECTED:
        if _cooldown_ok('rejected', order.id, CHEQUE_CUSTOMER_COOLDOWN):
            notify(phone, 'cheque_rejected_customer', name=name, order_id=order.id, reason=_sms_text(reason, 80),
                   deadline_note=deadline_note(order), cancel_note=_cancel_note(order))
    elif status == ChequePayment.STATUS_APPROVED:
        # فقط وقتی همه‌ی چک‌های فعال تأیید شد (با چند چک، به‌ازای هر تأیید پیامک نمی‌رود)
        pending = order.cheques.exclude(status__in=(ChequePayment.STATUS_APPROVED, ChequePayment.STATUS_WITHDRAWN)).exists()
        if not pending and _cooldown_ok('approved', order.id, CHEQUE_CUSTOMER_COOLDOWN):
            notify(phone, 'cheque_approved_customer', name=name, order_id=order.id)


@receiver(cheque_deadline_expired, dispatch_uid='notify_cheque_deadline_expired')
def on_cheque_deadline_expired(sender, order, **kwargs):
    phone = _valid_phone(order.user)
    if not phone:
        return
    notify(phone, 'cheque_deadline_canceled_customer', name=order.user.first_name or '', order_id=order.id)


# --- درخواست خرید چکی / اعتباری (فاز F4): ثبت (به مدیر)، تأیید و رد با علت (به مشتری) ---
# همان قواعد پیامک چک: ناهمگام و پس از commit، فقط شماره‌ی معتبر، cooldown، قابل خاموش/روشن در پنل. رویدادها از سرویس
# accounts/cheque_credit_service.py می‌آیند؛ پس ویوی مشتری، فرم/اکشن‌های ادمین و هر مسیر دیگر پوشش داده می‌شود.

@receiver(cheque_credit_requested, dispatch_uid='notify_cheque_credit_requested')
def on_cheque_credit_requested(sender, request, **kwargs):
    if not _cooldown_ok('credit_requested', request.user_id, CHEQUE_ADMIN_COOLDOWN):
        return
    name = _sms_text(f'{request.first_name} {request.last_name}'.strip(), 40)
    _notify_store_admins('cheque_credit_requested_admin', name=name, phone=request.user.phone_number)


@receiver(cheque_credit_approved, dispatch_uid='notify_cheque_credit_approved')
def on_cheque_credit_approved(sender, request, **kwargs):
    phone = _valid_phone(request.user)
    if phone and _cooldown_ok('credit_approved', request.user_id, CHEQUE_CUSTOMER_COOLDOWN):
        notify(phone, 'cheque_credit_approved_customer', name=request.user.first_name or '')


@receiver(cheque_credit_rejected, dispatch_uid='notify_cheque_credit_rejected')
def on_cheque_credit_rejected(sender, request, **kwargs):
    phone = _valid_phone(request.user)
    if phone and _cooldown_ok('credit_rejected', request.user_id, CHEQUE_CUSTOMER_COOLDOWN):
        notify(phone, 'cheque_credit_rejected_customer', name=request.user.first_name or '', reason=_sms_text(request.rejection_reason, 80))
