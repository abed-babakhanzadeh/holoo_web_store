"""
متن همه‌ی پیام‌های خروجی سایت، در یک جا.

قبلاً این متن‌ها داخل ویوها و تسک‌ها هاردکد بودند (payments/views.py، accounts/views.py،
holoo/tasks.py)؛ برای تغییر یک جمله باید چند فایل دست می‌خورد. حالا ویرایش متن = ویرایش
همین فایل، بدون دست‌زدن به هیچ منطقی.

هر قالب یک رشته‌ی format است. کلیدهای لازمش در required مشخص شده تا اگر فراخوان‌کننده
پارامتری را جا انداخت، در همان لحظه خطای واضح بگیریم نه یک پیام ناقص برای مشتری.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class MessageTemplate:
    body: str
    required: tuple = ()

    def render(self, context):
        missing = [key for key in self.required if key not in context]
        if missing:
            raise KeyError(f"پارامترهای لازم قالب پیام موجود نیستند: {', '.join(missing)}")
        return self.body.format(**context)


TEMPLATES = {
    # --- حساب کاربری ---
    'otp': MessageTemplate(
        'کد تایید شما برای ورود به فروشگاه: {code}',
        required=('code',),
    ),
    'user_registered_admin': MessageTemplate(
        'مدیر گرامی، شماره موبایل جدیدی ({phone}) در سایت ثبت‌نام کرد.',
        required=('phone',),
    ),
    'profile_completed_admin': MessageTemplate(
        'مدیر گرامی، مشتری جدید ({full_name} - {phone}) پروفایل خود را تکمیل کرد. '
        'لطفاً سطح قیمت ایشان را در هلو یا پنل بررسی نمایید.',
        required=('full_name', 'phone'),
    ),
    'user_resubmitted_admin': MessageTemplate(
        'مدیر گرامی، مشتری ردشده ({full_name} - {phone}) اطلاعات خود را ویرایش و درخواست بررسی مجدد ارسال کرده است.',
        required=('full_name', 'phone'),
    ),
    'account_approved_customer': MessageTemplate(
        '{name} گرامی، حساب کاربری شما تأیید شد؛ اکنون می‌توانید قیمت‌ها را مشاهده و خرید کنید. '
        'بازرگانی موسوی',
        required=('name',),
    ),

    # --- سفارش ---
    'order_placed_customer': MessageTemplate(
        '{name} گرامی، سفارش شما با شماره {order_id} با موفقیت ثبت شد و در حال پردازش می‌باشد. '
        'بازرگانی موسوی',
        required=('name', 'order_id'),
    ),
    'order_shipped_customer': MessageTemplate(
        '{name} گرامی، سفارش شما تحویل پست گردید. کد رهگیری پستی شما: {tracking_code} می‌باشد. '
        'بازرگانی موسوی',
        required=('name', 'tracking_code'),
    ),

    # --- پرداخت ---
    'payment_succeeded_customer': MessageTemplate(
        'مشتری گرامی {name}، پرداخت مبلغ {amount} تومان با موفقیت انجام شد. کد پیگیری: {ref_id}',
        required=('name', 'amount', 'ref_id'),
    ),
    'payment_succeeded_admin': MessageTemplate(
        'تراکنش جدید! سفارش #{order_id} به مبلغ {amount} تومان توسط {phone} با موفقیت پرداخت شد.',
        required=('order_id', 'amount', 'phone'),
    ),

    # --- موجودی ---
    'back_in_stock_sms': MessageTemplate(
        'کاربر گرامی، کالای «{product_name}» دوباره موجود شد. بازرگانی موسوی',
        required=('product_name',),
    ),
    'back_in_stock_email': MessageTemplate(
        '{name} گرامی،\n'
        'کالای «{product_name}» که برای اطلاع از موجود شدنش ثبت‌نام کرده بودید، هم‌اکنون در فروشگاه هلو موجود است.\n'
        'فروشگاه اینترنتی هلو',
        required=('name', 'product_name'),
    ),

    # --- هشدارهای عملیاتی ---
    'critical_alert': MessageTemplate(
        '🚨 خطای بحرانی در سایت: {message}',
        required=('message',),
    ),
    'holoo_sync_stalled_admin': MessageTemplate(
        '⚠️ سفارش #{order_id}: بیش از {days} روز است {what} در هلو انجام نشده. '
        'تلاش خودکار ادامه دارد اما بررسی دستی لازم است.',
        required=('order_id', 'days', 'what'),
    ),
}


def render_message(template_key, context):
    template = TEMPLATES.get(template_key)
    if template is None:
        raise KeyError(f"قالب پیام «{template_key}» تعریف نشده است.")
    return template.render(context)
