from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone
import re

IRAN_MOBILE_REGEX = re.compile(r"^9\d{9}$")


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

    number = re.sub(r"\D", "", str(phone_number).strip())

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
 
    # --- فیلدهای آدرس و تماس  ---
    state = models.CharField(max_length=50, blank=True, null=True, verbose_name='استان')
    city = models.CharField(max_length=50, blank=True, null=True, verbose_name='شهر')
    postal_code = models.CharField(max_length=10, blank=True, null=True, verbose_name='کد پستی')
    address = models.TextField(blank=True, null=True, verbose_name='آدرس دقیق')
    
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
    def is_profile_complete(self):
        return bool(
            self.first_name and 
            self.last_name and 
            self.national_code and 
            self.state and 
            self.city and 
            self.address
        )

    class Meta:
        verbose_name = 'کاربر'
        verbose_name_plural = 'کاربران'

    def __str__(self):
        name = f"{self.first_name or ''} {self.last_name or ''}".strip()
        return f"{name if name else self.phone_number} ({self.get_status_display()})"

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
    
    # این متد به انتهای کلاس OTPRequest اضافه می‌شود
    @classmethod
    def verify_code(cls, phone_number, code):
        """
        منطق بررسی صحت و انقضای کد تایید.
        خروجی: (وضعیت موفقیت: bool, پیام خطا یا کاربر: str/None)
        """
        # ۱. پیدا کردن آخرین کد مصرف نشده
        otp_req = cls.objects.filter(
            phone_number=phone_number,
            purpose=OTPPurpose.REGISTER_LOGIN,
            used_at__isnull=True
        ).order_by('-created_at').first()

        # ۲. بررسی صحت کد
        if not otp_req or otp_req.code != code:
            return False, "کد وارد شده نادرست است."
            
        # ۳. بررسی انقضای زمان ذخیره شده در دیتابیس
        if timezone.now() > otp_req.expires_at:
            return False, "کد تایید منقضی شده است. لطفا مجددا درخواست کد کنید."
            
        # ۴. تایید موفق و مصرف کد
        otp_req.used_at = timezone.now()
        otp_req.save()
        return True, None