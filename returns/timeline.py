"""
نمایش وضعیت یک درخواست مرجوعی برای مشتری (صفحه‌ی جزئیات مرجوعی): تایم‌لاین چهارمرحله‌ای و خلاصه‌ی هزینه‌ی بازگشت.

ترتیب مراحل از ماشین وضعیت واقعی می‌آید (returns.services): ابتدا فروشگاه درخواست را تأیید/رد می‌کند (decided_at)،
سپس مشتری کالا را می‌فرستد و فروشگاه دریافتش را ثبت می‌کند (item_received_at)، بعد بازرسی و بازپرداخت (completed_at).
وضعیت REFUND_PENDING زمان‌مهر جدا ندارد؛ همان مرحله‌ی «بازپرداخت» با برچسب «در صف بازپرداخت» جاری است.
"""
from .models import ReturnReason, ReturnRequest

STATE_DONE = 'done'
STATE_CURRENT = 'current'
STATE_PENDING = 'pending'
STATE_FAILED = 'failed'
STATE_SKIPPED = 'skipped'


def build_timeline(return_request):
    """ لیست چهار مرحله: [{key, label, state, date, badge, note}] """
    rr = return_request
    status = rr.status
    rejected = status == ReturnRequest.STATUS_REJECTED

    # --- مرحله ۱: ثبت درخواست (همیشه انجام‌شده)
    submitted = {'key': 'submitted', 'label': 'ثبت درخواست', 'state': STATE_DONE, 'date': rr.requested_at, 'badge': '', 'note': ''}

    # --- مرحله ۲: بررسی و تعیین نتیجه (تأیید/رد)
    if rejected:
        review = {'key': 'review', 'label': 'نتیجه‌ی بررسی درخواست', 'state': STATE_FAILED, 'date': rr.decided_at,
                  'badge': 'درخواست رد شد', 'note': rr.rejection_reason}
    elif status == ReturnRequest.STATUS_PENDING:
        review = {'key': 'review', 'label': 'نتیجه‌ی بررسی درخواست', 'state': STATE_CURRENT, 'date': None,
                  'badge': '', 'note': 'در انتظار بررسی کارشناس'}
    else:
        review = {'key': 'review', 'label': 'نتیجه‌ی بررسی درخواست', 'state': STATE_DONE, 'date': rr.decided_at,
                  'badge': 'درخواست تأیید شد', 'note': ''}

    # --- مرحله ۳: دریافت کالا توسط فروشگاه
    if rr.item_received_at:
        received = {'key': 'received', 'label': 'دریافت کالا توسط فروشگاه', 'state': STATE_DONE, 'date': rr.item_received_at,
                    'badge': 'کالاها دریافت شدند', 'note': ''}
    elif rejected:
        received = {'key': 'received', 'label': 'دریافت کالا توسط فروشگاه', 'state': STATE_SKIPPED, 'date': None, 'badge': '', 'note': ''}
    elif status == ReturnRequest.STATUS_APPROVED:
        received = {'key': 'received', 'label': 'دریافت کالا توسط فروشگاه', 'state': STATE_CURRENT, 'date': None,
                    'badge': '', 'note': 'منتظر ارسال کالا توسط شما'}
    else:
        received = {'key': 'received', 'label': 'دریافت کالا توسط فروشگاه', 'state': STATE_PENDING, 'date': None, 'badge': '', 'note': ''}

    # --- مرحله ۴: بازپرداخت و تکمیل
    if status == ReturnRequest.STATUS_COMPLETED:
        refund = {'key': 'refund', 'label': 'بازپرداخت و تکمیل نهایی', 'state': STATE_DONE, 'date': rr.completed_at,
                  'badge': 'بازپرداخت انجام شد', 'note': ''}
    elif rejected:
        refund = {'key': 'refund', 'label': 'بازپرداخت و تکمیل نهایی', 'state': STATE_SKIPPED, 'date': None, 'badge': '', 'note': ''}
    elif status == ReturnRequest.STATUS_REFUND_PENDING:
        refund = {'key': 'refund', 'label': 'بازپرداخت و تکمیل نهایی', 'state': STATE_CURRENT, 'date': None,
                  'badge': '', 'note': 'در صف بازپرداخت'}
    elif status == ReturnRequest.STATUS_ITEM_RECEIVED:
        refund = {'key': 'refund', 'label': 'بازپرداخت و تکمیل نهایی', 'state': STATE_CURRENT, 'date': None,
                  'badge': '', 'note': 'در حال بازرسی نهایی کالا'}
    else:
        refund = {'key': 'refund', 'label': 'بازپرداخت و تکمیل نهایی', 'state': STATE_PENDING, 'date': None, 'badge': '', 'note': ''}

    return [submitted, review, received, refund]


def shipping_summary(return_request, items):
    """
    وضعیت هزینه‌ی پست برگشتِ کالا: دلیل هر قلم تعیین می‌کند پرداخت‌کننده مشتری است یا فروشگاه.
    items باید از قبل با reason بارگذاری شده باشد. خروجی: {'free': bool|None, 'label': str, 'refund_note': str}
    """
    payers = {item.reason.shipping_cost_payer for item in items}
    if payers == {ReturnReason.PAYER_STORE}:
        free, label = True, 'رایگان (هزینه‌ی ارسال کالا به فروشگاه با فروشگاه است)'
    elif payers == {ReturnReason.PAYER_CUSTOMER}:
        free, label = False, 'عادی (هزینه‌ی ارسال کالا به فروشگاه با شماست)'
    elif payers:
        free, label = None, 'برای بخشی از اقلام با فروشگاه و برای بقیه با شماست (بسته به دلیل هر قلم)'
    else:
        free, label = None, ''
    return {'free': free, 'label': label, 'refunded': bool(return_request.shipping_refunded),
            'refund_amount': return_request.shipping_refund_amount}
