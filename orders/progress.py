"""
نگاشت وضعیت سفارش به «وضعیت جاری + نوار پیشرفت + مرحله‌ی بعد» برای کارت مرسوله‌ی جزئیات سفارش (مثل دیجی‌کالا).

وضعیت جاری با رنگ سبز بالای نوار، و زیر نوار «مرحله بعد: …» با خاکستری ملایم می‌آید. برای سفارش تحویل‌شده مرحله‌ی بعد
نیست و فقط تیک سبز «تحویل مرسوله به مشتری» با ۱۰۰٪ می‌ماند. سفارش لغوشده مرسوله‌ی در حال پیشرفت ندارد (None).

مبنا status خام سفارش است (نه customer_status): سفارش چکی هرگز تراکنش آنلاین ندارد و is_paid آن همیشه False است، ولی
وقتی انبار آن را ارسال/تحویل کرد باید همان را نشان دهیم؛ پرداخت‌شدن فقط بین «در انتظار پرداخت» و «در انتظار پردازش»
(وضعیت‌های pending/registered) فرق می‌گذارد.
"""

# percent: پیشرفت نوار (۰ تا ۱۰۰)؛ مقدارهای کوچکِ مراحل اول برای این است که نوار خالی دیده نشود
_AWAITING_PAYMENT = ('در انتظار پرداخت / بررسی', 'تأیید و پردازش سفارش', 5)
_AWAITING_PROCESSING = ('در انتظار پردازش', 'آماده‌سازی سفارش', 20)
_PREPARING = ('در حال آماده‌سازی در انبار', 'تحویل به مامور ارسال / پست', 50)
_SHIPPED = ('ارسال شده / تحویل به پست', 'تحویل به مشتری', 75)
_DELIVERED = ('تحویل مرسوله به مشتری', '', 100)
_UNDER_REVIEW = ('در انتظار تأیید مدیر / در حال بررسی', 'تأیید و آماده‌سازی سفارش', 10)
_STOCK_ISSUE = ('نیازمند هماهنگی (اتمام موجودی)', 'هماهنگی با پشتیبانی', 5)


def shipment_progress(order):
    """ {'title', 'next_label', 'percent', 'done'} یا None برای سفارش لغوشده """
    status = order.status
    if status == 'canceled':
        return None
    if status == 'delivered':
        title, next_label, percent = _DELIVERED
    elif status == 'shipped':
        title, next_label, percent = _SHIPPED
    elif status == 'processing':
        title, next_label, percent = _PREPARING
    elif status == 'rejected_stock':
        title, next_label, percent = _STOCK_ISSUE
    elif order.approved_at:                                     # تأییدشده؛ pending / registered
        title, next_label, percent = _AWAITING_PROCESSING
    elif order.is_paid or order.settled_off_site:               # پرداخت‌شده/چکی، منتظر تأیید مدیر
        title, next_label, percent = _UNDER_REVIEW
    else:
        title, next_label, percent = _AWAITING_PAYMENT
    return {'title': title, 'next_label': next_label, 'percent': percent, 'done': status == 'delivered'}
