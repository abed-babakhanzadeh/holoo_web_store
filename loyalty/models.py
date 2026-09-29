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

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import CheckConstraint, Q

from accounts.models import CustomUser

from .exceptions import LedgerImmutableError, LoyaltyTierDeletionError


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


class LoyaltyTier(models.Model):
    """
    سطح داینامیک باشگاه مشتریان (Loyalty Phase 3A). کاملاً مستقل از CustomUser.LOYALTY_LEVELS/
    get_loyalty_level (accounts/models.py) - آن سیستم زنده و ۵ سطح ثابتش در این فاز دست‌نخورده
    می‌ماند (مصوبه‌ی صریح فاز ۳: بدون کات‌اوور، بدون کپی آستانه‌های قدیمی). رتبه‌بندی این مدل
    منحصراً بر مبنای LoyaltyAccount.lifetime_earned (فاز ۱/۲) خواهد بود - نگاه کنید
    loyalty/services.py:get_tier_for_lifetime_points.

    مثل LoyaltyAccount/LoyaltyTransaction، حذف فیزیکی مسدود است (delete() پایین) - فقط
    غیرفعال‌سازی (is_active=False) مجاز است، چون یک سطح ممکن است از جای دیگری (مثلاً فازهای
    بعدی promotions) با rank آن ارجاع داده شده باشد.
    """
    title = models.CharField(max_length=50, unique=True, verbose_name='نام سطح')
    rank = models.PositiveSmallIntegerField(unique=True, verbose_name='رتبه سطح')
    threshold = models.PositiveIntegerField(unique=True, verbose_name='حداقل امتیاز کسب‌شده')
    is_active = models.BooleanField(default=True, verbose_name='فعال')
    badge_color = models.CharField(max_length=20, blank=True, default='', verbose_name='رنگ نشان')
    # Loyalty Phase 5B-1 - نگاشت صریح (Crosswalk) به مقیاس صلب سنتی؛ صرفاً داده، بدون هیچ اثر
    # اجرایی - loyalty/stats.py::effective_loyalty_index هنوز این فیلد را نمی‌خواند (موکول به ۵B-2).
    legacy_equivalent_index = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MinValueValidator(0), MaxValueValidator(4)],
        verbose_name='اندیس معادل سنتی (۰ تا ۴)',
        help_text='معادل این سطح در مقیاس ۵‌سطحی سنتی جهت انطباق با قوانین تخفیف و کوپن‌ها. '
                  'در صورت خالی بودن، فاقد معادل لحاظ می‌شود.',
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='تاریخ ایجاد')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='آخرین تغییر')

    class Meta:
        verbose_name = 'سطح باشگاه مشتریان'
        verbose_name_plural = 'سطوح باشگاه مشتریان'
        ordering = ('rank',)
        indexes = [
            # منطبق بر کوئری ارزیابی سطح کاربر: filter(is_active=True, threshold__lte=points)
            # نام کوتاه عمدی: محدودیت جنگو حداکثر ۳۰ نویسه برای نام ایندکس (models.E034)
            models.Index(fields=['is_active', 'threshold'], name='loyaltytier_active_idx'),
        ]

    def __str__(self):
        return f'{self.title} (رتبه {self.rank})'

    def clean(self):
        """
        اعتبارسنجی بین‌ردیفی (Cross-Row): هرچه rank بالاتر، threshold باید اکیداً بزرگ‌تر باشد.
        چون این یک قید بین چند ردیف است (نه یک ستون تک‌ردیفی)، در سطح دیتابیس با یک
        CheckConstraint ساده قابل بیان نیست - دقیقاً هم‌دلیلی که SiteSettings.clean() برای
        صعودی‌بودن ۴ آستانه‌ی ثابتش دارد (products/models.py)، اینجا برای N ردیف تعمیم یافته.
        روی کل مجموعه (سایر سطوح + همین نمونه، جایگزین نسخه‌ی قبلی خودش) اجرا می‌شود تا هم
        رکورد جدید هم ویرایش رکورد موجود را بگیرد.
        """
        super().clean()
        if self.rank is None or self.threshold is None:
            return
        others = LoyaltyTier.objects.exclude(pk=self.pk).order_by('rank')
        combined = sorted([*others, self], key=lambda tier: tier.rank)
        for previous, current in zip(combined, combined[1:]):
            if current.threshold <= previous.threshold:
                raise ValidationError({
                    'threshold': (
                        f'آستانه‌ی سطح باید اکیداً صعودی باشد: سطح «{current.title}» (رتبه {current.rank}، '
                        f'آستانه {current.threshold}) باید آستانه‌ی بیشتری از سطح «{previous.title}» '
                        f'(رتبه {previous.rank}، آستانه {previous.threshold}) داشته باشد.'
                    )
                })

        # Loyalty Phase 5B-1 - اعتبارسنجی کراس‌واک (صرفاً داده‌ای؛ هنوز هیچ کد اجرایی نمی‌خواندش):
        # combined در این نقطه از رتبه/آستانه صعودی مطمئن است (چک بالا رد نکرد)، پس combined[0]
        # همان سطح پایه (کمترین آستانه) است.
        base_tier = combined[0]
        if base_tier.legacy_equivalent_index is not None and base_tier.legacy_equivalent_index != 0:
            raise ValidationError({
                'legacy_equivalent_index': (
                    f'سطح پایه («{base_tier.title}»، کمترین آستانه) فقط می‌تواند اندیس معادل سنتی ۰ داشته باشد یا '
                    'این فیلد را خالی بگذارد؛ مقدار غیرصفر کف واجدشرایطی مهمان/کاربر تازه‌وارد را ناخواسته بالا می‌برد.'
                )
            })

        mapped = [tier for tier in combined if tier.legacy_equivalent_index is not None]
        for previous, current in zip(mapped, mapped[1:]):
            if current.legacy_equivalent_index < previous.legacy_equivalent_index:
                raise ValidationError({
                    'legacy_equivalent_index': (
                        f'اندیس معادل سنتی باید غیرنزولی باشد: سطح «{current.title}» (آستانه {current.threshold}) '
                        f'نمی‌تواند اندیس معادلی کمتر از سطح «{previous.title}» (آستانه {previous.threshold}) داشته باشد.'
                    )
                })

    def delete(self, *args, **kwargs):
        raise LoyaltyTierDeletionError(
            'سطوح باشگاه مشتریان قابل حذف فیزیکی نیستند؛ به‌جای حذف، فیلد «فعال» را خاموش کنید.'
        )


class LoyaltyReward(models.Model):
    """
    یک آیتم کاتالوگ پاداش (Loyalty Phase 4B) - نگاشت «هزینه‌ی امتیازی» به یک promotions.Coupon
    از پیش تعریف‌شده (الگوی Master/Template Coupon، مصوبه‌ی صریح فاز ۴B: بدون تولید کد تازه
    به‌ازای هر بازخرید). OneToOneField عمداً نه ForeignKey: هر Coupon دقیقاً به یک ردیف کاتالوگ
    پاداش متصل است تا ابهامِ «کدام پاداش صاحب این کد است» از اساس رخ ندهد.

    محدودیتِ ذاتیِ این الگو (مستندشده در گزارش ممیزی ۴B): چون promotions.UserCoupon قید
    UniqueConstraint(coupon, user) دارد، هر کاربر حداکثر یک‌بار در کل عمرش می‌تواند این پاداش
    مشخص را بازخرید کند - این عمداً یک محدودیتِ پذیرفته‌شده است، نه نقص. ظرفیت کلی/سقف هر کاربر
    عمداً روی این مدل تکرار نشده - از فیلدهای موجود Coupon.claim_limit/total_limit خوانده
    می‌شود (نگاه کنید loyalty/reward_redemption.py) تا دو منبع حقیقت برای یک عدد ایجاد نشود.
    """
    title = models.CharField(max_length=100, verbose_name='عنوان پاداش در کاتالوگ')
    coupon = models.OneToOneField(
        'promotions.Coupon', on_delete=models.PROTECT, related_name='loyalty_reward', verbose_name='کد تخفیف مرتبط',
        help_text='کوپنی که با خرج امتیاز به کاربر تخصیص می‌یابد. باید مخاطب «فقط کاربران تعریف‌شده» و '
                  'غیرِ«قابل‌دریافت در پنل» باشد.',
    )
    points_cost = models.PositiveIntegerField(
        validators=[MinValueValidator(1)], verbose_name='هزینه‌ی امتیازی',
        help_text='تعداد امتیازی که برای دریافت این پاداش از حساب کاربر کسر می‌شود.',
    )
    is_active = models.BooleanField(default=True, verbose_name='فعال (قابل بازخرید)')
    display_order = models.PositiveSmallIntegerField(default=0, verbose_name='اولویت نمایش')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='تاریخ ایجاد')

    class Meta:
        verbose_name = 'پاداش باشگاه مشتریان'
        verbose_name_plural = 'پاداش‌های باشگاه مشتریان'
        ordering = ('display_order', 'id')
        constraints = [
            CheckConstraint(condition=Q(points_cost__gte=1), name='loyaltyreward_points_cost_gte_1'),
        ]

    def __str__(self):
        return f'{self.title} ({self.points_cost} امتیاز)'
