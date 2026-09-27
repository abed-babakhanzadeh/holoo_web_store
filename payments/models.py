from django.db import models
from django.db.models import CheckConstraint, Q
from accounts.models import CustomUser
from orders.models import Order

class Transaction(models.Model):
    STATUS_CHOICES = (
        ('pending', 'در انتظار پرداخت'),
        ('success', 'پرداخت موفق'),
        ('failed', 'پرداخت ناموفق'),
    )

    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='transactions', verbose_name='کاربر')
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='transactions', verbose_name='سفارش')

    # amount = مبلغی که واقعاً به درگاه ارسال/از آن تأیید شده (Wallet-only: صفر؛ Mixed: باقی‌مانده
    # پس از کسر سهم کیف‌پول؛ Gateway-only: کل سفارش). برای «مبلغ کلی این تراکنش» از total_amount
    # پایین استفاده شود، نه این فیلد به‌تنهایی.
    amount = models.DecimalField(max_digits=12, decimal_places=0, verbose_name='مبلغ (تومان)')
    authority = models.CharField(max_length=100, unique=True, verbose_name='کد ارجاع درگاه (Authority)')
    ref_id = models.CharField(max_length=100, blank=True, null=True, verbose_name='شماره پیگیری بانک (RefID)')

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name='وضعیت')

    # --- Wallet Phase 4: پرداخت ترکیبی کیف‌پول + درگاه ---
    # سهم کیف‌پول از این سفارش. is_paid/تسویه‌ی هلو کماکان فقط به «وجود یک Transaction موفق»
    # وابسته‌اند (نگاه کنید orders.models.Order.is_paid) - این فیلد صرفاً برای گزارش/نمایش و برای
    # چک مغایرت مبلغ درگاه (amount == order.total_price - wallet_amount) استفاده می‌شود.
    wallet_amount = models.DecimalField(
        max_digits=12, decimal_places=0, default=0, verbose_name='سهم کیف‌پول',
    )
    # ردیف لجرِ واقعی که سهم کیف‌پول را کسر کرد (wallet.services.debit_wallet). PROTECT: تا این
    # تراکنش وجود دارد، آن ردیف لجر (سابقه‌ی مالی) قابل حذف نیست.
    wallet_transaction = models.ForeignKey(
        'wallet.WalletTransaction', on_delete=models.PROTECT, null=True, blank=True,
        related_name='+', verbose_name='ردیف لجر کیف‌پول',
    )
    # نقطه‌ی حقیقت واحد Idempotency برای بازگشت سهم کیف‌پول؛ فقط توسط
    # payments.checkout._reverse_wallet_leg_locked نوشته می‌شود - نه جای دیگری. سه مسیر (شکست/
    # انصراف درگاه، انقضای ۳۰ دقیقه‌ای، لغو سفارش) هرکدام قبل از Reverse کردن این را چک می‌کنند
    # تا شارژ/برگشت تکراری کیف‌پول ساختاری غیرممکن باشد، نه صرفاً به‌شرط عدم خطا.
    wallet_reversed_at = models.DateTimeField(
        null=True, blank=True, verbose_name='زمان بازگشت سهم کیف‌پول',
    )

    created_at = models.DateTimeField(auto_now_add=True, verbose_name='تاریخ ایجاد')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='آخرین تغییر')

    class Meta:
        verbose_name = 'تراکنش'
        verbose_name_plural = 'تراکنش‌ها'
        constraints = [
            CheckConstraint(condition=Q(wallet_amount__gte=0), name='transaction_wallet_amount_gte_0'),
            # بازگشت وجه بدون سهم کیف‌پول منطقاً بی‌معناست
            CheckConstraint(
                condition=~Q(wallet_reversed_at__isnull=False) | Q(wallet_amount__gt=0),
                name='transaction_reversed_requires_wallet_amount',
            ),
        ]

    def __str__(self):
        return f"{self.authority} - {self.status}"

    @property
    def total_amount(self):
        """ مبلغ کلی که این تراکنش نمایانگرش است (درگاه + کیف‌پول)؛ برای نمایش به کاربر همیشه
        این را بخوانید، نه amount خام (که برای Wallet-only صفر و برای Mixed فقط سهم درگاه است). """
        return self.amount + self.wallet_amount
