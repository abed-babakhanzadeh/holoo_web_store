from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.db.models import F
from django.utils import timezone
from django.utils.functional import cached_property
import re
import secrets

from services.storage import OverwriteStorage
from services.text import to_latin_digits

IRAN_MOBILE_REGEX = re.compile(r"^9\d{9}$")


def avatar_upload_path(instance, filename):
    """ نام فایل آواتار همیشه شناسه‌ی عددی کاربر است، تا هر کاربر دقیقاً یک فایل آواتار داشته باشد """
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'jpg'
    return f'avatars/{instance.id}.{ext}'


def normalize_phone_number(phone_number: str) -> str:
    """
    Normalize Iranian mobile number to:

        09XXXXXXXXX

    Accepted formats:

        09123456789
        9123456789
        +989123456789
        989123456789
        00989123456789
        0989123456789
        09-1234-56789
        0912 345 6789
        (0912)3456789
    """

    if phone_number is None:
        raise ValueError("وارد کردن شماره موبایل الزامی است.")

    # ارقام فارسی/عربی (۰۹۱۲... یا ٠٩١٢...) قبل از پاک‌سازی به لاتین تبدیل می‌شوند، وگرنه \D
    # (که یونیکد-آگاه است) آن‌ها را رقم تشخیص می‌دهد و رگکس نهایی fail می‌کند
    number = re.sub(r"\D", "", to_latin_digits(str(phone_number).strip()))

    while True:
        old = number

        if number.startswith("0098"):
            number = number[4:]

        elif number.startswith("098"):
            number = number[3:]

        elif number.startswith("98"):
            number = number[2:]

        elif number.startswith("0") and len(number) > 10:
            number = number[1:]

        if old == number:
            break

    if not IRAN_MOBILE_REGEX.fullmatch(number):
        raise ValueError("شماره موبایل وارد شده معتبر نیست (مثال صحیح: 09123456789).")

    return "0" + number

# 1. Enum وضعیت‌ها (ماشین وضعیت استاندارد Enterprise)
class UserStatus(models.TextChoices):
    PENDING_PROFILE = 'PENDING_PROFILE', 'نیازمند تکمیل اطلاعات'
    PENDING_ERP_SYNC = 'PENDING_ERP_SYNC', 'در انتظار همگام‌سازی هلو'
    ACTIVE = 'ACTIVE', 'مشتری فعال'
    REJECTED = 'REJECTED', 'حساب مسدود'

# 2. مدیریت کاستوم یوزر (هندل کردن لاگین با موبایل و بدون پسورد)
class CustomUserManager(BaseUserManager):
    def create_user(self, phone_number, password=None, **extra_fields):
        if not phone_number:
            raise ValueError("شماره موبایل الزامی است.")

        # نرمال‌سازی و اعتبارسنجی شماره موبایل
        phone_number = normalize_phone_number(phone_number)

        user = self.model(
            phone_number=phone_number,
            **extra_fields
        )

        if password:
            user.set_password(password)
        else:
            # برای سیستم OTP-Only
            user.set_unusable_password()

        user.save(using=self._db)
        return user

    def create_superuser(self, phone_number, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("status", UserStatus.ACTIVE)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")

        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        return self.create_user(phone_number, password, **extra_fields)

# 3. مدل اصلی کاربر
# 3. مدل اصلی کاربر (اضافه شدن فیلدهای هلو)
class CustomUser(AbstractBaseUser, PermissionsMixin):
    phone_number = models.CharField(max_length=11, unique=True, verbose_name='شماره موبایل')
    
    # --- فیلدهای جدید برای پروفایل ---
    first_name = models.CharField(max_length=50, blank=True, null=True, verbose_name='نام')
    last_name = models.CharField(max_length=50, blank=True, null=True, verbose_name='نام خانوادگی')
    # کد ملی برای اشخاص حقیقی در هلو الزامی یا بسیار مهم است
    national_code = models.CharField(max_length=10, blank=True, null=True, verbose_name='کد ملی')
    email = models.EmailField(blank=True, null=True, verbose_name='پست الکترونیک')
    birth_date = models.DateField(blank=True, null=True, verbose_name='تاریخ تولد')
    avatar = models.ImageField(upload_to=avatar_upload_path, storage=OverwriteStorage(), blank=True, null=True, verbose_name='تصویر پروفایل')

    # موجودی کیف پول (فقط نمایشی؛ شارژ/برداشت واقعی هنوز پیاده نشده)
    wallet_balance = models.DecimalField(max_digits=12, decimal_places=0, default=0, verbose_name='موجودی کیف پول')

    # آدرس‌ها در مدل جدا و چندتایی نگه‌داری می‌شوند: accounts.Address (related_name='addresses')

    # سطح قیمت کاربر برای اتصال به قیمت‌های 1 تا 10 هلو
    PRICE_LEVELS = [(i, f'قیمت فروش {i}') for i in range(1, 11)]
    price_level = models.PositiveSmallIntegerField(
        choices=PRICE_LEVELS, 
        default=1, 
        verbose_name='سطح قیمت پیش‌فرض'
    )
    
    # تغییر دیفالت وضعیت به PENDING_PROFILE
    status = models.CharField(
        max_length=20, 
        choices=UserStatus.choices, 
        default=UserStatus.PENDING_PROFILE, 
        verbose_name='وضعیت'
    )
    
    # کد هلو
    erp_code = models.CharField(max_length=50, blank=True, null=True, db_index=True, verbose_name='کد هلو')

    # شناسه‌ی پایدار گوگل (claim: sub) برای اتصال ورود با گوگل به همین حساب.
    # عمداً unique=True نگذاشتیم: دیتابیس SQL Server است و چندین مقدار NULL در یک ایندکس یکتا خطا می‌دهد؛
    # یکتا بودن را در سطح ویو (GoogleLoginCallbackView) با کوئری چک می‌کنیم.
    google_sub = models.CharField(max_length=255, blank=True, null=True, db_index=True, verbose_name='شناسه گوگل')
    
    # فیلدهای رهگیری خطای یکپارچه‌سازی 
    retry_count = models.PositiveSmallIntegerField(default=0, verbose_name='تعداد تلاش مجدد')
    last_sync_error = models.TextField(blank=True, null=True, verbose_name='آخرین خطای ارتباط با هلو')
    
    is_active = models.BooleanField(default=True, verbose_name='فعال')
    is_staff = models.BooleanField(default=False, verbose_name='دسترسی کارمند')
    date_joined = models.DateTimeField(default=timezone.now, verbose_name='تاریخ عضویت')

    objects = CustomUserManager()

    USERNAME_FIELD = 'phone_number'
    REQUIRED_FIELDS = []
    
    def save(self, *args, **kwargs):
        if self.phone_number:
            self.phone_number = normalize_phone_number(self.phone_number)
        super().save(*args, **kwargs)

    # متدی برای بررسی اینکه آیا کاربر پروفایلش را کامل کرده یا نه
    # متدی برای بررسی اینکه آیا کاربر پروفایلش را کامل کرده یا نه
    def has_real_password(self):
        """
        بررسی دقیق‌تر از has_usable_password() استاندارد جنگو: آن متد فقط چک می‌کند رمز با
        set_unusable_password() (پیشوند '!') غیرفعال نشده باشد، ولی کاربرانی که از مسیر قدیمی
        get_or_create (بدون create_user) ساخته شده‌اند مقدار password خالی ('') دارند که طبق
        has_usable_password() همچنان «قابل استفاده» شمرده می‌شود. این متد آن حالت را هم رد می‌کند.
        """
        return bool(self.password) and self.has_usable_password()

    def is_profile_complete(self):
        # آدرس دیگر جزو پروفایل نیست؛ از صفحه‌ی «آدرس‌ها» یا هنگام تسویه‌حساب ثبت می‌شود
        return bool(self.first_name and self.last_name and self.national_code)

    @cached_property
    def default_address(self):
        """ آدرس پیش‌فرض کاربر یا None (کاربری که آدرسی ندارد پیش‌فرض هم ندارد). در طول یک درخواست کش می‌شود. """
        return self.addresses.filter(is_default=True).select_related('city', 'city__province', 'zone').first()

    # آستانه‌های سطح مشتری بر اساس تعداد سفارش‌های موفق (پرداخت‌شده) واقعی کاربر
    LOYALTY_LEVELS = (
        (0, 'مشتری جدید'),
        (3, 'برنزی'),
        (7, 'نقره‌ای'),
        (15, 'طلایی'),
        (30, 'الماسی'),
    )

    class Meta:
        verbose_name = 'کاربر'
        verbose_name_plural = 'کاربران'

    def __str__(self):
        name = f"{self.first_name or ''} {self.last_name or ''}".strip()
        return f"{name if name else self.phone_number} ({self.get_status_display()})"

    @cached_property
    def paid_orders_count(self):
        """
        تعداد سفارش‌های پرداخت‌شده‌ی کاربر، از رجیستری آمار (تأمین‌کننده‌اش اپ orders است).

        cached_property چون قالب‌ها چند بار پشت سر هم get_loyalty_* را صدا می‌زنند؛ قبلاً هر
        کدام از این سه متد کوئری یکسانِ خودش را می‌زد (۳ کوئری تکراری در هر لود پیشخوان و
        ۴ تا در صفحه‌ی پروفایل). حالا در هر درخواست فقط یک بار محاسبه می‌شود.
        """
        from .stats import get
        return get('orders_paid_count', self, 0) or 0

    def _loyalty_bounds(self):
        """ (آستانه‌ی سطح فعلی، برچسب سطح فعلی، آستانه‌ی سطح بعدی، برچسب سطح بعدی) """
        current_threshold, current_label = self.LOYALTY_LEVELS[0]
        next_threshold, next_label = None, None
        for threshold, label in self.LOYALTY_LEVELS:
            if self.paid_orders_count >= threshold:
                current_threshold, current_label = threshold, label
            else:
                next_threshold, next_label = threshold, label
                break
        return current_threshold, current_label, next_threshold, next_label

    def get_loyalty_points(self):
        """ امتیاز وفاداری: هر سفارش پرداخت‌شده = ۱۰۰ امتیاز """
        return self.paid_orders_count * 100

    def get_loyalty_level(self):
        """ خروجی: (نام سطح فعلی، سطح بعدی یا None، تعداد سفارش تا سطح بعد) """
        _, current_label, next_threshold, next_label = self._loyalty_bounds()
        remaining = (next_threshold - self.paid_orders_count) if next_threshold else 0
        return current_label, next_label, remaining

    def get_loyalty_progress_percent(self):
        """ درصد پیشرفت واقعی کاربر تا سطح بعدی مشتری، برای نوار پیشرفت در پروفایل/پیشخوان """
        current_threshold, _, next_threshold, _ = self._loyalty_bounds()
        if not next_threshold or next_threshold <= current_threshold:
            return 100
        progress = (self.paid_orders_count - current_threshold) / (next_threshold - current_threshold) * 100
        return max(0, min(100, round(progress)))

# 4. Enum دلایل OTP
class OTPPurpose(models.TextChoices):
    REGISTER_LOGIN = 'LOGIN', 'ثبت‌نام / ورود'
    RESET_PASSWORD = 'RESET', 'بازیابی رمز'

# 5. مدل ردیابی و امنیت OTP
class OTPRequest(models.Model):
    phone_number = models.CharField(max_length=11, verbose_name='شماره موبایل')
    code = models.CharField(max_length=6, verbose_name='کد تایید')
    purpose = models.CharField(
        max_length=10, 
        choices=OTPPurpose.choices, 
        default=OTPPurpose.REGISTER_LOGIN,
        verbose_name='هدف'
    )
    
    # فیلدهای امنیتی برای جلوگیری از Brute Force
    ip_address = models.GenericIPAddressField(blank=True, null=True, verbose_name='آدرس IP')
    attempt_count = models.PositiveSmallIntegerField(default=0, verbose_name='تعداد تلاش اشتباه')
    
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='تاریخ ایجاد')
    expires_at = models.DateTimeField(verbose_name='تاریخ انقضا')
    used_at = models.DateTimeField(blank=True, null=True, verbose_name='تاریخ استفاده')

    class Meta:
        verbose_name = 'درخواست کد یکبار مصرف'
        verbose_name_plural = 'درخواست‌های کد یکبار مصرف'
        # ایندکس برای سرعت بالاتر در جستجوی آخرین پیامک کاربر
        indexes = [
            models.Index(fields=['phone_number', 'created_at']),
        ]

    def __str__(self):
        return f"{self.phone_number} - {self.code}"

    # از این تعداد تلاش اشتباه به بعد، برای تلاش‌های بعدی روی همین کد، کپچا لازم می‌شود
    CAPTCHA_THRESHOLD = 3

    @classmethod
    def _latest(cls, phone_number, purpose):
        return cls.objects.filter(
            phone_number=phone_number, purpose=purpose, used_at__isnull=True
        ).order_by('-created_at').first()

    @classmethod
    def captcha_required(cls, phone_number, purpose=OTPPurpose.REGISTER_LOGIN):
        """ بدون مصرف کردن چیزی، فقط چک می‌کند که آیا برای تلاش بعدی روی این کد، کپچا لازم است """
        otp_req = cls._latest(phone_number, purpose)
        return bool(otp_req and otp_req.attempt_count >= cls.CAPTCHA_THRESHOLD)

    # این متد به انتهای کلاس OTPRequest اضافه می‌شود
    @classmethod
    def verify_code(cls, phone_number, code, purpose=OTPPurpose.REGISTER_LOGIN):
        """
        منطق بررسی صحت و انقضای کد تایید.
        پارامتر purpose برای استفاده‌ی مجدد این متد در مسیر «بازیابی رمز عبور» اضافه شده
        (پیش‌فرض همان رفتار قبلی یعنی ورود/ثبت‌نام را حفظ می‌کند).
        خروجی: (وضعیت موفقیت: bool, پیام خطا یا None, تعداد تلاش اشتباه فعلی روی این کد)
        """
        # ۱. پیدا کردن آخرین کد مصرف نشده
        otp_req = cls._latest(phone_number, purpose)

        # ۲. بررسی صحت کد
        if not otp_req or not secrets.compare_digest(str(otp_req.code), str(code or '')):
            if otp_req:
                # افزایش اتمیک در سطح دیتابیس: با otp_req.attempt_count += 1 (خواندن، جمع،
                # نوشتن) چند تلاش موازی روی یک کد می‌توانستند شمارنده را عقب نگه دارند و
                # از آستانه‌ی کپچا رد شوند
                cls.objects.filter(pk=otp_req.pk).update(attempt_count=F('attempt_count') + 1)
                otp_req.refresh_from_db(fields=['attempt_count'])
            return False, "کد وارد شده نادرست است.", (otp_req.attempt_count if otp_req else 0)

        # ۳. بررسی انقضای زمان ذخیره شده در دیتابیس
        if timezone.now() > otp_req.expires_at:
            return False, "کد تایید منقضی شده است. لطفا مجددا درخواست کد کنید.", otp_req.attempt_count

        # ۴. مصرف کد به‌صورت اتمیک: شرط used_at__isnull=True داخل خودِ UPDATE است، پس اگر
        # دو درخواست هم‌زمان با یک کد درست برسند، فقط یکی‌شان موفق می‌شود و کد دوبار
        # استفاده نمی‌شود
        consumed = cls.objects.filter(pk=otp_req.pk, used_at__isnull=True).update(used_at=timezone.now())
        if not consumed:
            return False, "این کد قبلاً استفاده شده است. لطفا مجددا درخواست کد کنید.", otp_req.attempt_count

        return True, None, otp_req.attempt_count

# مدل آدرس در ماژول جدا تعریف شده تا این فایل بزرگ‌تر نشود؛ اینجا وارد می‌شود تا جنگو آن را ثبت کند
from .address import Address  # noqa: E402,F401
