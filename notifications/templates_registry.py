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
    title: str
    body: str
    required: tuple = ()
    # وضعیتِ اولیه‌ی «فعال» در تنظیمات ادمین؛ پیام‌های تازه‌ای که برای مشتری‌ها خودکار و گروهی می‌روند (مثل یادآوری) با
    # False شروع می‌شوند تا ادمین آگاهانه روشنشان کند
    default_enabled: bool = True

    def render(self, context):
        missing = [key for key in self.required if key not in context]
        if missing:
            raise KeyError(f"پارامترهای لازم قالب پیام موجود نیستند: {', '.join(missing)}")
        return self.body.format(**context)


TEMPLATES = {
    # --- حساب کاربری ---
    'otp': MessageTemplate(
        title='کد تأیید ورود (OTP)',
        body='کد تایید شما برای ورود به فروشگاه: {code}',
        required=('code',),
    ),
    'user_registered_admin': MessageTemplate(
        title='ثبت‌نام کاربر جدید (به ادمین)',
        body='مدیر گرامی، شماره موبایل جدیدی ({phone}) در سایت ثبت‌نام کرد.',
        required=('phone',),
    ),
    'profile_completed_admin': MessageTemplate(
        title='تکمیل پروفایل مشتری (به ادمین)',
        body='مدیر گرامی، مشتری جدید ({full_name} - {phone}) پروفایل خود را تکمیل کرد. '
             'لطفاً سطح قیمت ایشان را در هلو یا پنل بررسی نمایید.',
        required=('full_name', 'phone'),
    ),
    'user_resubmitted_admin': MessageTemplate(
        title='ویرایش مجدد پروفایل ردشده (به ادمین)',
        body='مدیر گرامی، مشتری ردشده ({full_name} - {phone}) اطلاعات خود را ویرایش و درخواست بررسی مجدد ارسال کرده است.',
        required=('full_name', 'phone'),
    ),
    'profile_incomplete_reminder_customer': MessageTemplate(
        title='یادآوری تکمیل پروفایل (به مشتری)',
        body='{name} گرامی، برای مشاهده‌ی قیمت‌ها و ثبت سفارش، لطفاً نام و کد ملی خود را در پروفایل سایت تکمیل کنید. '
             'بازرگانی موسوی',
        required=('name',),
        default_enabled=False,
    ),
    'account_approved_customer': MessageTemplate(
        title='تأیید حساب کاربری (به مشتری)',
        body='{name} گرامی، حساب کاربری شما تأیید شد؛ اکنون می‌توانید قیمت‌ها را مشاهده و خرید کنید. '
             'بازرگانی موسوی',
        required=('name',),
    ),

    # --- سفارش ---
    'order_placed_customer': MessageTemplate(
        title='ثبت سفارش جدید (به مشتری)',
        body='{name} گرامی، سفارش شما با شماره {order_id} با موفقیت ثبت شد و در حال پردازش می‌باشد. '
             'بازرگانی موسوی',
        required=('name', 'order_id'),
    ),
    'order_approved_customer': MessageTemplate(
        title='تأیید سفارش توسط مدیر (به مشتری)',
        body='{name} گرامی، سفارش شما با شماره {order_id} تأیید شد و در حال آماده‌سازی است. بازرگانی موسوی',
        required=('name', 'order_id'),
    ),
    'order_rejected_stock_customer': MessageTemplate(
        title='رد شدن سفارش به‌دلیل نبود موجودی (به مشتری)',
        body='{name} گرامی، متأسفانه به‌دلیل اتمام موجودی، سفارش شما با شماره {order_id} تأیید نشد. {refund_note}'
             'بازرگانی موسوی',
        required=('name', 'order_id', 'refund_note'),
    ),
    'order_shipped_customer': MessageTemplate(
        title='ارسال سفارش با کد رهگیری (به مشتری)',
        body='{name} گرامی، سفارش شما تحویل پست گردید. کد رهگیری پستی شما: {tracking_code} می‌باشد. '
             'بازرگانی موسوی',
        required=('name', 'tracking_code'),
    ),

    # --- سفارش چکی (فاز E) ---
    # deadline_note: «ظرف ۲۴ ساعت» یا «در اسرع وقت»؛ cancel_note: جمله‌ی «در غیر این صورت سفارش لغو می‌شود.» یا خالی (مهلت خاموش)
    'cheque_order_placed_customer': MessageTemplate(
        title='ثبت سفارش چکی و لزوم ثبت اطلاعات چک (به مشتری)',
        body='{name} گرامی، سفارش چکی شما با شماره {order_id} ثبت شد. لطفاً {deadline_note} اطلاعات و تصویر چک را از بخش '
             '«سفارش‌های من» در سایت ثبت کنید.{cancel_note} بازرگانی موسوی',
        required=('name', 'order_id', 'deadline_note', 'cancel_note'),
    ),
    'cheque_registered_admin': MessageTemplate(
        title='ثبت یا اصلاح چک، منتظر بررسی (به ادمین)',
        body='مدیر گرامی، چکِ سفارش #{order_id} از کاربر {phone} {action} و منتظر بررسی شماست.',
        required=('order_id', 'phone', 'action'),
    ),
    'cheque_approved_customer': MessageTemplate(
        title='تأیید چک (به مشتری)',
        body='{name} گرامی، مدارک چک سفارش {order_id} تأیید شد؛ سفارش شما به‌زودی توسط کارشناس تأیید نهایی و برای '
             'آماده‌سازی و ارسال به انبار ارجاع می‌شود. بازرگانی موسوی',
        required=('name', 'order_id'),
    ),
    'cheque_rejected_customer': MessageTemplate(
        title='رد چک و لزوم اصلاح (به مشتری)',
        body='{name} گرامی، مدارک چک سفارش {order_id} تأیید نشد. علت: {reason}. لطفاً {deadline_note} چک را از بخش '
             '«سفارش‌های من» اصلاح یا چک جایگزین ثبت کنید.{cancel_note} بازرگانی موسوی',
        required=('name', 'order_id', 'reason', 'deadline_note', 'cancel_note'),
    ),
    'cheque_deadline_canceled_customer': MessageTemplate(
        title='لغو خودکار سفارش چکی به‌دلیل پایان مهلت (به مشتری)',
        body='{name} گرامی، سفارش شما با شماره {order_id} به‌دلیل پایان مهلت ثبت یا اصلاح چک لغو شد و کالاهای رزروشده '
             'آزاد گردید. در صورت تمایل می‌توانید سفارش جدید ثبت کنید. بازرگانی موسوی',
        required=('name', 'order_id'),
    ),

    # --- پرداخت ---
    'payment_succeeded_customer': MessageTemplate(
        title='پرداخت موفق (به مشتری)',
        body='مشتری گرامی {name}، پرداخت مبلغ {amount} تومان با موفقیت انجام شد. کد پیگیری: {ref_id}',
        required=('name', 'amount', 'ref_id'),
    ),
    'payment_succeeded_admin': MessageTemplate(
        title='پرداخت موفق (به ادمین)',
        body='تراکنش جدید! سفارش #{order_id} به مبلغ {amount} تومان توسط {phone} با موفقیت پرداخت شد.',
        required=('order_id', 'amount', 'phone'),
    ),

    # --- گفتگوی آنلاین ---
    'chat_offline_message_admin': MessageTemplate(
        title='پیام جدید در گفتگوی آنلاین (به ادمین)',
        body='مدیر گرامی، پیام جدیدی در گفتگوی آنلاین سایت از «{name}» ثبت شد؛ لطفاً پیشخوان گفتگو را بررسی کنید.',
        required=('name',),
    ),
    'chat_reply_customer': MessageTemplate(
        title='پاسخ کارشناس در گفتگوی آنلاین (به مشتری)',
        body='{name} گرامی، کارشناس ما به پیام شما در گفتگوی آنلاین پاسخ داد؛ برای مشاهده‌ی پاسخ وارد سایت شوید. بازرگانی موسوی',
        required=('name',),
    ),

    # --- موجودی ---
    'back_in_stock_sms': MessageTemplate(
        title='اطلاع موجود شدن کالا - پیامک (به مشتری)',
        body='کاربر گرامی، کالای «{product_name}» دوباره موجود شد. بازرگانی موسوی',
        required=('product_name',),
    ),
    'back_in_stock_email': MessageTemplate(
        title='اطلاع موجود شدن کالا - ایمیل (به مشتری)',
        body='{name} گرامی،\n'
             'کالای «{product_name}» که برای اطلاع از موجود شدنش ثبت‌نام کرده بودید، هم‌اکنون در فروشگاه هلو موجود است.\n'
             'فروشگاه اینترنتی هلو',
        required=('name', 'product_name'),
    ),

    # --- تماس با ما ---
    'contact_message_admin': MessageTemplate(
        title='پیام جدید از فرم تماس با ما (به ادمین)',
        body='مدیر گرامی، پیام جدیدی از «{name}» با موضوع «{subject}» در بخش تماس با ما ثبت شد. '
             'لطفاً از پنل مدیریت بررسی نمایید.',
        required=('name', 'subject'),
    ),

    # --- هشدارهای عملیاتی ---
    'critical_alert': MessageTemplate(
        title='هشدار خطای بحرانی سایت (به ادمین)',
        body='🚨 خطای بحرانی در سایت: {message}',
        required=('message',),
    ),
    'order_needs_attention_admin': MessageTemplate(
        title='رد شدن ثبت فاکتور در هلو، نیازمند بررسی (به ادمین)',
        body='مدیر گرامی، ثبت فاکتور سفارش #{order_id} در هلو رد شد و نیاز به بررسی دستی دارد: {message}',
        required=('order_id', 'message'),
    ),
    'order_rejected_stock_admin': MessageTemplate(
        title='رد شدن سفارش به‌دلیل نبود موجودی (به ادمین)',
        body='مدیر گرامی، سفارش #{order_id} به‌دلیل نبود موجودی رد شد. {paid_note}',
        required=('order_id', 'paid_note'),
    ),
    'holoo_sync_stalled_admin': MessageTemplate(
        title='توقف همگام‌سازی با هلو (به ادمین)',
        body='⚠️ سفارش #{order_id}: بیش از {days} روز است {what} در هلو انجام نشده. '
             'تلاش خودکار ادامه دارد اما بررسی دستی لازم است.',
        required=('order_id', 'days', 'what'),
    ),

    # --- کیف پول ---
    # نکته‌ی امنیتی: پیامک‌های *مشتری* هرگز نباید شماره کارت/شبا را در متن داشته باشند
    # (کاربر خودش شماره‌اش را می‌داند؛ SMS کانال امنی برای افشای دوباره‌ی آن نیست). فقط پیامک
    # مدیر (که برای واریز دستی به این اطلاعات نیاز دارد) این‌ها را دارد.
    'withdrawal_requested_admin': MessageTemplate(
        title='درخواست برداشت جدید (به ادمین)',
        body='مدیر گرامی، درخواست برداشت جدید از کاربر {phone} به مبلغ {amount} تومان ثبت شد. '
             'شماره کارت: {card} - شبا: {iban}. لطفاً ظرف ۲۴ تا ۴۸ ساعت بررسی و تسویه نمایید.',
        required=('phone', 'amount', 'card', 'iban'),
    ),
    'withdrawal_requested_customer': MessageTemplate(
        title='ثبت درخواست برداشت (به مشتری)',
        body='{name} گرامی، درخواست برداشت شما به مبلغ {amount} تومان ثبت شد و در صف بررسی قرار گرفت.',
        required=('name', 'amount'),
    ),
    'withdrawal_approved_customer': MessageTemplate(
        title='تأیید درخواست برداشت (به مشتری)',
        body='{name} گرامی، درخواست برداشت شما به مبلغ {amount} تومان تأیید شد و طی ۲۴ الی ۴۸ ساعت '
             'آینده به حساب اعلام‌شده واریز خواهد شد.',
        required=('name', 'amount'),
    ),
    'withdrawal_paid_customer': MessageTemplate(
        title='پرداخت شدن برداشت (به مشتری)',
        body='{name} گرامی، مبلغ {amount} تومان درخواست برداشت شما با موفقیت به حساب شما واریز شد.',
        required=('name', 'amount'),
    ),
    'withdrawal_rejected_customer': MessageTemplate(
        title='رد شدن درخواست برداشت (به مشتری)',
        body='{name} گرامی، متأسفانه درخواست برداشت شما به مبلغ {amount} تومان رد شد. دلیل: {reason}',
        required=('name', 'amount', 'reason'),
    ),

    # --- مرجوعی کالا ---
    'return_requested_customer': MessageTemplate(
        title='ثبت درخواست مرجوعی (به مشتری)',
        body='{name} گرامی، درخواست مرجوعی شما برای سفارش #{order_id} ثبت شد و در صف بررسی قرار گرفت.',
        required=('name', 'order_id'),
    ),
    'return_requested_admin': MessageTemplate(
        title='ثبت درخواست مرجوعی (به ادمین)',
        body='مدیر گرامی، درخواست مرجوعی جدید برای سفارش #{order_id} از کاربر {phone} ثبت شد.',
        required=('order_id', 'phone'),
    ),
    'return_approved_customer': MessageTemplate(
        title='تأیید درخواست مرجوعی (به مشتری)',
        body='{name} گرامی، درخواست مرجوعی شما برای سفارش #{order_id} تأیید شد؛ لطفاً کالا را طبق '
             'راهنمای ارسال، برگشت بزنید.',
        required=('name', 'order_id'),
    ),
    'return_item_received_customer': MessageTemplate(
        title='دریافت کالای مرجوعی (به مشتری)',
        body='{name} گرامی، کالای مرجوعی شما برای سفارش #{order_id} به دست ما رسید و در حال بازرسی است؛ '
             'نتیجه را به‌زودی اطلاع می‌دهیم.',
        required=('name', 'order_id'),
    ),
    'return_refund_pending_customer': MessageTemplate(
        title='قرار گرفتن مرجوعی در صف بازپرداخت (به مشتری)',
        body='{name} گرامی، بازرسی کالای مرجوعی سفارش #{order_id} انجام شد و مبلغ {amount} تومان در صف بازپرداخت '
             'قرار گرفت.',
        required=('name', 'order_id', 'amount'),
    ),
    'return_rejected_customer': MessageTemplate(
        title='رد شدن درخواست مرجوعی (به مشتری)',
        body='{name} گرامی، متأسفانه درخواست مرجوعی شما برای سفارش #{order_id} رد شد. دلیل: {reason}',
        required=('name', 'order_id', 'reason'),
    ),
    'return_refund_completed_customer': MessageTemplate(
        title='تکمیل بازپرداخت مرجوعی (به مشتری)',
        body='{name} گرامی، بازپرداخت مرجوعی سفارش #{order_id} به مبلغ {amount} تومان با موفقیت به {destination} '
             'شما انجام شد.',
        required=('name', 'order_id', 'amount', 'destination'),
    ),

    # --- باشگاه مشتریان ---
    'loyalty_tier_upgraded_customer': MessageTemplate(
        title='ارتقای سطح باشگاه مشتریان (به مشتری)',
        body='تبریک! سطح شما در باشگاه مشتریان به «{tier_title}» ارتقا یافت.',
        required=('tier_title',),
    ),
}


def render_message(template_key, context, body_override=None):
    """
    body_override: متن جای‌گزینِ ادمین (NotificationSetting.custom_body) به‌جای body پیش‌فرض؛
    اعتبارسنجی required همچنان روی همان پارامترهای قالب اصلی انجام می‌شود تا با متن جای‌گزین هم
    یک KeyError واضح بگیریم، نه پیام ناقص.
    """
    template = TEMPLATES.get(template_key)
    if template is None:
        raise KeyError(f"قالب پیام «{template_key}» تعریف نشده است.")
    if body_override:
        missing = [key for key in template.required if key not in context]
        if missing:
            raise KeyError(f"پارامترهای لازم قالب پیام موجود نیستند: {', '.join(missing)}")
        return body_override.format(**context)
    return template.render(context)
