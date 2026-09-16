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
    'profile_completed_admin': MessageTemplate(
        'مدیر گرامی، مشتری جدید ({full_name} - {phone}) پروفایل خود را تکمیل کرد. '
        'لطفاً سطح قیمت ایشان را در هلو یا پنل بررسی نمایید.',
        required=('full_name', 'phone'),
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
