"""
هسته‌ی داده‌ای باشگاه مشتریان (Loyalty Phase 1 — فقط مدل‌های دفترکل؛ بدون اتصال خودکار به
پرداخت سفارش/مرجوعی و بدون تغییر Tier جاری).

تفکیک عمدی از سطح‌بندی فعلی (مصوبه‌ی ممیزی فاز صفر): CustomUser.LOYALTY_LEVELS/
get_loyalty_points (accounts/models.py) یک «سطح نمایشی محاسبه‌شونده» است که هرگز در این اپ
خوانده یا نوشته نمی‌شود؛ LoyaltyAccount/LoyaltyTransaction اینجا یک «ارز قابل‌خرج» با دفترکل
واقعی‌اند - دو محور کاملاً مستقل (Loyalty Tier در برابر Loyalty Points Ledger).

هر تغییر current_balance/lifetime_earned/lifetime_redeemed و افزودن LoyaltyTransaction فقط
باید از طریق loyalty/services.py انجام شود؛ هیچ کد دیگری (نه ادمین، نه ویو، نه شل) نباید
مستقیم این فیلدها را بنویسد - دقیقاً همان قرارداد wallet/models.py.
"""

from django.db import models
from django.db.models import CheckConstraint, Q

from accounts.models import CustomUser

from .exceptions import LedgerImmutableError


class LoyaltyAccount(models.Model):
    """
    یک‌به‌یک با کاربر. current_balance کش هم‌گام‌شده‌ی مجموع جبری LoyaltyTransaction.amount است
    (دقیقاً همان توجیه wallet.models.Wallet.balance) - برای خواندن سریع بدون SUM روی کل
    دفترکل در هر مصرف. on_delete=PROTECT مثل Wallet.user: تا کاربری تاریخچه‌ی امتیاز دارد،
    اصلاً قابل‌حذف نیست.
    """
    user = models.OneToOneField(
        CustomUser, on_delete=models.PROTECT, related_name='loyalty_account', verbose_name='کاربر',
    )
    current_balance = models.PositiveIntegerField(default=0, verbose_name='موجودی فعلی امتیاز')
    lifetime_earned = models.PositiveIntegerField(
        default=0, verbose_name='مجموع امتیاز کسب‌شده (کل تاریخچه)',
        help_text='با خرج‌کردن/کسر کاهش نمی‌یابد؛ فقط با کسب امتیاز جدید افزایش می‌یابد.',
    )
    lifetime_redeemed = models.PositiveIntegerField(default=0, verbose_name='مجموع امتیاز خرج‌شده (کل تاریخچه)')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='تاریخ ایجاد')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='آخرین فعالیت')

    class Meta:
        verbose_name = 'حساب باشگاه مشتریان'
        verbose_name_plural = 'حساب‌های باشگاه مشتریان'
        constraints = [
            CheckConstraint(condition=Q(current_balance__gte=0), name='loyaltyaccount_current_balance_gte_0'),
        ]

    def __str__(self):
        return f'حساب باشگاه {self.user.phone_number}'

    @classmethod
    def get_or_create_for_user(cls, user):
        account, _ = cls.objects.get_or_create(user=user)
        return account


class LoyaltyTransaction(models.Model):
    """
    دفترکل امتیاز - غیرقابل‌ویرایش و غیرقابل‌حذف (نگاه کنید save()/delete() پایین). اصلاح یک
    تراکنش همیشه با ثبت یک تراکنش جدید (معمولاً از نوع REVERSE) انجام می‌شود، نه ویرایش رکورد
    موجود - دقیقاً همان قرارداد wallet.models.WalletTransaction.
    """
    EARN_ORDER = 'EARN_ORDER'
    EARN_ACTION = 'EARN_ACTION'
    ADMIN_CREDIT = 'ADMIN_CREDIT'
    ADMIN_DEBIT = 'ADMIN_DEBIT'
    REDEEM_WALLET = 'REDEEM_WALLET'
    REDEEM_REWARD = 'REDEEM_REWARD'
    REVERSE = 'REVERSE'
    EXPIRE = 'EXPIRE'
    TRANSACTION_TYPE_CHOICES = (
        (EARN_ORDER, 'کسب از خرید'),
        (EARN_ACTION, 'کسب از سایر فعالیت‌ها'),
        (ADMIN_CREDIT, 'اعطای دستی توسط مدیر'),
        (ADMIN_DEBIT, 'کسر دستی توسط مدیر'),
        (REDEEM_WALLET, 'تبدیل به کیف پول'),
        (REDEEM_REWARD, 'خرید پاداش/کوپن'),
        (REVERSE, 'برگشت امتیاز (لغو/مرجوعی)'),
        (EXPIRE, 'انقضای امتیاز'),
    )

    account = models.ForeignKey(
        LoyaltyAccount, on_delete=models.PROTECT, related_name='transactions', verbose_name='حساب باشگاه',
    )
    amount = models.IntegerField(verbose_name='مقدار (علامت‌دار: مثبت=کسب/واریز، منفی=خرج/کسر)')
    # اسنپ‌شات موجودی بلافاصله بعد از این ردیف - برای ممیزی/گزارش سریع بدون نیاز به SUM پنجره‌ای روی کل تاریخچه
    balance_after = models.PositiveIntegerField(verbose_name='موجودی پس از این تراکنش')
    transaction_type = models.CharField(max_length=15, choices=TRANSACTION_TYPE_CHOICES, verbose_name='نوع تراکنش')

    # رفرنس باز برای فازهای بعدی (سفارش، مرجوعی، کوپن پاداش، ...) بدون نیاز به تغییر هسته‌ی مالی؛
    # عمداً دو ستون ساده (نه GenericForeignKey/ContentType) - هم‌الگوی wallet.models.WalletTransaction
    source_type = models.CharField(max_length=50, blank=True, default='', verbose_name='نوع منبع')
    source_id = models.PositiveIntegerField(null=True, blank=True, verbose_name='شناسه‌ی منبع')

    idempotency_key = models.CharField(
        max_length=128, null=True, blank=True, unique=True, verbose_name='کلید ضدتکرار',
        help_text='برای تراکنش‌های سیستمی (نه دستیِ ادمین)؛ جلوگیری از ثبت دوباره‌ی یک رویداد.',
    )
    expires_at = models.DateTimeField(
        null=True, blank=True, verbose_name='زمان انقضا',
        help_text='آمادگی معماری برای انقضای دوره‌ای/FIFO در فازهای بعدی؛ در Phase 1 ذخیره می‌شود ولی هیچ‌جا اجرا نمی‌شود.',
    )
    remaining_amount = models.PositiveIntegerField(
        default=0, verbose_name='باقی‌مانده‌ی قابل‌مصرف',
        help_text='فقط برای تراکنش‌های کسبِ مثبت (Earn) معنا دارد؛ برای پشتیبانی از کسر FIFO در فازهای بعدی.',
    )
    reason = models.CharField(max_length=255, verbose_name='علت تراکنش')
    created_by = models.ForeignKey(
        CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='+', verbose_name='ثبت‌کننده',
        help_text='برای تراکنش‌های دستی: ادمین عامل. برای تراکنش‌های سیستمی: خالی.',
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='تاریخ ثبت')

    class Meta:
        verbose_name = 'تراکنش باشگاه مشتریان'
        verbose_name_plural = 'تراکنش‌های باشگاه مشتریان'
        ordering = ('-created_at', '-id')
        constraints = [
            CheckConstraint(condition=Q(balance_after__gte=0), name='loyaltytransaction_balance_after_gte_0'),
        ]

    def __str__(self):
        return f'{self.get_transaction_type_display()} ({self.amount:+}) - حساب #{self.account_id}'

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise LedgerImmutableError(
                'رکوردهای دفترکل امتیاز تغییرناپذیرند. برای اصلاح، به‌جای ویرایش این رکورد یک '
                'تراکنش جدید (مثلاً از نوع REVERSE) با loyalty.services ثبت کنید.'
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise LedgerImmutableError('رکوردهای دفترکل امتیاز قابل‌حذف نیستند.')
