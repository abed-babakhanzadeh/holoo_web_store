"""
تنها نقطه‌ی اتصال نرم‌افزار حسابداری به بقیه‌ی پروژه.

هیچ اپ دیگری holoo را import نمی‌کند؛ این فایل به رویدادهای دامنه گوش می‌دهد و تسک‌های
مربوطه را شلیک می‌کند. برای جایگزینی هلو با هر ERP دیگری کافیست یک اپ مشابه با همین سه
شنونده ساخته و اپ holoo از INSTALLED_APPS برداشته شود — orders / payments / accounts
حتی یک خط تغییر نمی‌کنند.
"""

import logging

from django.dispatch import receiver

from accounts.signals import default_address_changed, profile_completed, profile_updated
from orders.signals import order_approved
from payments.signals import payment_succeeded
from returns.signals import return_refund_queued, return_rejected

from .tasks import (confirm_payment_in_holoo, flag_rejected_return_with_invoice, send_order_to_holoo, send_return_to_holoo,
                    sync_user_to_holoo)

logger = logging.getLogger(__name__)


@receiver(order_approved, dispatch_uid='holoo_send_order_on_approval')
def on_order_approved(sender, order, **kwargs):
    """
    ثبت فاکتور قطعی سفارش در حسابداری، فقط پس از «تأیید سفارش» توسط مدیر. ثبت سفارش و پرداخت (حتی موفق) به‌تنهایی
    فاکتور نمی‌سازد و انبار هلو را تغییر نمی‌دهد.
    """
    try:
        send_order_to_holoo.delay(order.id)
    except Exception:
        # صف در دسترس نیست؛ تسک دوره‌ای reconcile_holoo_orders بعداً این سفارش را برمی‌دارد
        logger.exception("شلیک تسک ثبت فاکتور سفارش %s ناموفق بود؛ در صف بازبینی می‌ماند.", order.id)


@receiver(return_refund_queued, dispatch_uid='holoo_send_return_on_refund_queued')
def on_return_refund_queued(sender, return_request, **kwargs):
    """
    ثبت فاکتور «برگشت از فروش» وقتی مرجوعی به صف بازپرداخت رسید: کالا دریافت و بازرسی شده و مبلغ‌ها قطعی‌اند (نه پیش‌تر که ممکن است
    رد شود یا تعداد عوض شود، و نه پس از پرداخت که موجودی و حساب هلو بی‌دلیل عقب می‌ماند).
    """
    try:
        send_return_to_holoo.delay(return_request.id)
    except Exception:
        logger.exception("شلیک تسک ثبت فاکتور برگشتی مرجوعی %s ناموفق بود؛ در صف بازبینی می‌ماند.", return_request.id)


@receiver(return_rejected, dispatch_uid='holoo_flag_rejected_return')
def on_return_rejected(sender, return_request, **kwargs):
    """ رد مرجوعی پس از ثبت فاکتور برگشتی (از صف بازپرداخت): هلو حذف برگشتی را پشتیبانی نمی‌کند؛ مدیر برای حذف دستی مطلع می‌شود """
    if not return_request.holoo_invoice_id:
        return
    try:
        flag_rejected_return_with_invoice(return_request.id)
    except Exception:
        logger.exception("علامت‌گذاری مرجوعی ردشده‌ی %s با فاکتور برگشتیِ ثبت‌شده در هلو ناموفق بود.", return_request.id)


@receiver(payment_succeeded, dispatch_uid='holoo_confirm_payment')
def on_payment_succeeded(sender, order, transaction, **kwargs):
    """ ثبت سند دریافت وجه برای فاکتور از قبل ثبت‌شده """
    try:
        confirm_payment_in_holoo.delay(order.id)
    except Exception:
        logger.exception("شلیک تسک ثبت سند دریافت وجه سفارش %s ناموفق بود؛ در صف بازبینی می‌ماند.", order.id)


@receiver(profile_completed, dispatch_uid='holoo_sync_user_completed')
@receiver(profile_updated, dispatch_uid='holoo_sync_user_updated')
def on_profile_changed(sender, user, **kwargs):
    """ ساخت/به‌روزرسانی مشتری در حسابداری """
    try:
        sync_user_to_holoo.delay(user.id)
    except Exception:
        logger.exception("شلیک تسک همگام‌سازی کاربر %s ناموفق بود.", user.id)


@receiver(default_address_changed, dispatch_uid='holoo_sync_user_default_address')
def on_default_address_changed(sender, user, **kwargs):
    """
    آدرس مشتری در هلو همان آدرس پیش‌فرض است؛ با تغییرش مشتری دوباره همگام می‌شود. کاربری که پروفایلش
    کامل نیست هنوز در هلو ساخته نشده (نام/کد ملی ندارد)؛ او هنگام تکمیل پروفایل، با آدرس پیش‌فرضِ
    همان لحظه همگام می‌شود، پس اینجا رد می‌شود.
    """
    if not user.is_profile_complete():
        return
    on_profile_changed(sender=sender, user=user)
