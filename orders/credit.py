"""
سقف اعتبار خرید چکی (فاز G1).

مرجع سقف: CustomUser.cheque_credit_limit — NULL = بدون سقف (پیش‌فرض و رفتار مشتریان چکیِ قدیمی)، عدد مثبت = سقف، ۰ = اعتبار فریز
(مجوز می‌ماند ولی سفارش چکیِ تازه ممکن نیست). با تأیید درخواست خرید چکی سقف تأییدشده روی کاربر نوشته می‌شود
(accounts/cheque_credit_service.py).

اعتبار درگیر (outstanding) = جمع Order.total_price سفارش‌های چکیِ کاربر در وضعیت‌های pending/registered/processing/shipped/delivered.
  - *زنده* از روی سفارش‌ها حساب می‌شود، نه شمارنده‌ی ذخیره‌شده: لغو دستی، لغو خودکار مهلت چک (فاز D) و رد برای نبود موجودی
    (canceled / rejected_stock در فرمول نیستند) اعتبار را خودکار آزاد می‌کنند و هیچ ناسازگاری‌ای ممکن نیست.
  - از لحظه‌ی ثبت سفارش (حتی بدون هیچ چک) می‌شمارد؛ وگرنه می‌شد چند سفارش چکیِ بی‌چک پشت هم ثبت کرد.
  - مبلغ مرجع، مبلغ سفارش است (مبلغ هر چک اختیاری است و قابل‌اتکا نیست).
  - آزادسازی با «وصول چک» (G3): سفارشی که Order.cheque_settled_at دارد (همه‌ی چک‌های فعالش cleared) از اعتبار درگیر خارج است.
  - مرجوعی (G3): برای سفارش‌های همین مجموعه، مبلغ قطعیِ مرجوعی‌های REFUND_PENDING و COMPLETED (جمع ReturnItem.refund_amount +
    ReturnRequest.shipping_refund_amount، یعنی همان total_refund_amount) کم می‌شود. این مبالغ فقط در لحظه‌ی گذار به REFUND_PENDING
    نوشته می‌شوند؛ مرجوعیِ در انتظار/تأییدشده/کالا‌رسیده هنوز مبلغ قطعی ندارد و اعتبار را آزاد نمی‌کند (محافظه‌کارانه). مرجوعیِ ردشده هیچ اثری ندارد.
    جمع نهایی هرگز منفی نمی‌شود.
  - فقط «سهم خریدهای سایت» دیده می‌شود؛ فروش حضوری مستقیم در هلو در این محاسبه نیست (طبق تصمیم کارفرما).

اعمال سمت سرور: credit_block() داخل تراکنش ثبت سفارش، اول ردیف کاربر را قفل می‌کند و بعد مصرف‌شده را می‌خواند؛ پس دو ثبت هم‌زمانِ یک کاربر
پشت هم صف می‌شوند و دومی سفارش اولی را می‌بیند. قفل‌ها: سبد ← کاربر (ترتیبِ بقیه‌ی مسیرها با این چرخه نمی‌سازد).
"""
from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Q, Sum

from accounts.models import CustomUser

from .models import Order
from .templatetags.money import money

OUTSTANDING_STATUSES = ('pending', 'registered', 'processing', 'shipped', 'delivered')
ZERO = Decimal('0')


@dataclass(frozen=True)
class CreditState:
    limit: Decimal | None            # None = بدون سقف
    used: Decimal                    # اعتبار درگیر

    @property
    def unlimited(self):
        return self.limit is None

    @property
    def frozen(self):
        return self.limit is not None and self.limit == 0

    @property
    def remaining(self):
        """ مانده‌ی اعتبار (هرگز منفی نیست)؛ None وقتی سقفی نیست """
        if self.limit is None:
            return None
        return max(self.limit - self.used, ZERO)

    def excess_for(self, amount):
        """ مبلغِ مازاد یک سفارش نسبت به مانده؛ ۰ وقتی جا می‌شود یا سقفی نیست """
        if self.limit is None:
            return ZERO
        return max(Decimal(amount) - self.remaining, ZERO)

    def allows(self, amount):
        return self.excess_for(amount) == 0


def _cheque_orders(user_pk):
    return Order.objects.filter(user_id=user_pk).filter(Q(settlement=Order.SETTLEMENT_CHEQUE) | Q(payment_method='check'))


REFUND_FINAL_STATUSES = ('REFUND_PENDING', 'COMPLETED')            # returns.models.ReturnRequest.STATUS_*؛ مبلغ قطعی از این گذار نوشته می‌شود


def outstanding_orders(user_pk):
    """ سفارش‌های چکیِ فعال و وصول‌نشده‌ی کاربر (مبنای اعتبار درگیر) """
    return _cheque_orders(user_pk).filter(status__in=OUTSTANDING_STATUSES, cheque_settled_at__isnull=True)


def refunded_total(user_pk):
    """ مبلغ قطعیِ مرجوعی‌های فعال/تکمیل‌شده‌ی همین سفارش‌های درگیر (هر سه کوئری بدون N+1؛ بدون سفارش درگیر، کوئری نمی‌خورد) """
    from returns.models import ReturnItem, ReturnRequest                # وارد کردن دیرهنگام: returns به orders وابسته است
    orders = outstanding_orders(user_pk).values('pk')
    items = ReturnItem.objects.filter(return_request__order__in=orders, return_request__status__in=REFUND_FINAL_STATUSES) \
        .aggregate(total=Sum('refund_amount'))['total']
    shipping = ReturnRequest.objects.filter(order__in=orders, status__in=REFUND_FINAL_STATUSES) \
        .aggregate(total=Sum('shipping_refund_amount'))['total']
    return Decimal(items or 0) + Decimal(shipping or 0)


def outstanding_total(user):
    """ جمع اعتبار درگیر کاربر: سفارش‌های چکیِ فعال و وصول‌نشده منهای مرجوعی‌های قطعی (حداقل ۰) """
    total = Decimal(outstanding_orders(user.pk).aggregate(total=Sum('total_price'))['total'] or 0)
    if total == 0:
        return ZERO
    return max(total - refunded_total(user.pk), ZERO)


def credit_state(user, *, limit=...):
    """
    وضعیت اعتبار کاربر. limit پیش‌فرض از خودِ نمونه‌ی user خوانده می‌شود (برای نمایش کافی است)؛ برای تصمیم‌گیری باید مقدارِ تازه از
    دیتابیس زیر قفل داده شود (credit_block).
    """
    if limit is ...:
        limit = user.cheque_credit_limit
    return CreditState(limit=None if limit is None else Decimal(limit), used=outstanding_total(user))


@dataclass(frozen=True)
class CreditCheck:
    """ وضعیت اعتبار یک کاربرِ دارای سقف نسبت به مبلغِ سفارشِ چکیِ جاری (برای نمایش در تسویه‌حساب؛ تصمیم نهایی با credit_block) """
    state: CreditState
    amount: Decimal

    @property
    def blocked(self):
        return not self.state.allows(self.amount)

    @property
    def excess(self):
        return self.state.excess_for(self.amount)


def credit_check(user, amount):
    """ CreditCheck برای کاربرِ دارای سقف؛ None وقتی سقفی ندارد (NULL = بدون سقف، هیچ کادری نمایش داده نمی‌شود) """
    if user.cheque_credit_limit is None:
        return None
    return CreditCheck(state=credit_state(user), amount=Decimal(amount))


def exceeded_message(state, amount):
    """ پیام فارسی شفافِ رد سفارش (مبلغ، مانده، مازاد) """
    if state.frozen:
        return 'اعتبار خرید چکی حساب شما فریز شده است؛ برای خرید چکی با پشتیبانی تماس بگیرید یا روش پرداخت نقدی را انتخاب کنید.'
    return (f'مبلغ این سفارش {money(amount)} تومان است ولی اعتبار چکی باقی‌مانده‌ی شما {money(state.remaining)} تومان است '
            f'(سقف {money(state.limit)}، مصرف‌شده {money(state.used)}؛ مازاد {money(state.excess_for(amount))} تومان). '
            'بخشی از سبد را کم کنید یا روش پرداخت نقدی را انتخاب کنید.')


def credit_block(user, amount):
    """
    باید داخل transaction.atomic صدا زده شود (ثبت سفارش). ردیف کاربر را قفل می‌کند، سقف را تازه از دیتابیس می‌خواند، مصرف‌شده را زیر
    همان قفل حساب می‌کند و اگر amount جا نشود پیام فارسی را برمی‌گرداند؛ وگرنه None (از جمله وقتی سقفی نیست).
    """
    locked = CustomUser.objects.select_for_update().get(pk=user.pk)
    if locked.cheque_credit_limit is None:
        return None
    state = credit_state(locked)
    if state.allows(amount):
        return None
    return exceeded_message(state, amount)
