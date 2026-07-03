from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone

# 1. Enum وضعیت‌ها (شامل حالت رد شده برای تکمیل بودن چرخه)
class UserStatus(models.TextChoices):
    PENDING = 'PENDING', 'در انتظار تایید ادمین'
    PROCESSING = 'PROCESSING', 'در حال پردازش (هلو)'
    APPROVED = 'APPROVED', 'تایید و ثبت شده'
    REJECTED = 'REJECTED', 'رد شده'

# 2. مدیریت کاستوم یوزر (هندل کردن لاگین با موبایل و بدون پسورد)
class CustomUserManager(BaseUserManager):
    def create_user(self, phone_number, password=None, **extra_fields):
        if not phone_number:
            raise ValueError('شماره موبایل الزامی است')
        
        # نرمال‌سازی شماره (بعداً می‌توانی تابع دقیق‌تری برای تبدیل +98 به 09 بنویسی)
        phone_number = phone_number.strip()
        
        user = self.model(phone_number=phone_number, **extra_fields)
        
        if password:
            user.set_password(password)
        else:
            # برای سیستم OTP-Only، پسورد غیرقابل استفاده تنظیم می‌شود تا امنیت حفظ شود
            user.set_unusable_password()
            
        user.save(using=self._db)
        return user

    def create_superuser(self, phone_number, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('status', UserStatus.APPROVED)
        return self.create_user(phone_number, password, **extra_fields)

# 3. مدل اصلی کاربر
class CustomUser(AbstractBaseUser, PermissionsMixin):
    phone_number = models.CharField(max_length=11, unique=True, verbose_name='شماره موبایل')
    status = models.CharField(
        max_length=15, 
        choices=UserStatus.choices, 
        default=UserStatus.PENDING, 
        verbose_name='وضعیت'
    )
    
    # کد هلو (توجه: unique=True در سطح جنگو برداشته شده تا باگ مقادیر NULL در SQL Server رخ ندهد)
    erp_code = models.CharField(max_length=50, blank=True, null=True, db_index=True, verbose_name='کد هلو')
    
    # فیلدهای رهگیری خطای یکپارچه‌سازی (جایگزین جدول پیچیده IntegrationJob برای MVP)
    retry_count = models.PositiveSmallIntegerField(default=0, verbose_name='تعداد تلاش مجدد')
    last_sync_error = models.TextField(blank=True, null=True, verbose_name='آخرین خطای ارتباط با هلو')
    
    is_active = models.BooleanField(default=True, verbose_name='فعال')
    is_staff = models.BooleanField(default=False, verbose_name='دسترسی کارمند')
    date_joined = models.DateTimeField(default=timezone.now, verbose_name='تاریخ عضویت')

    objects = CustomUserManager()

    USERNAME_FIELD = 'phone_number'
    REQUIRED_FIELDS = []

    class Meta:
        verbose_name = 'کاربر'
        verbose_name_plural = 'کاربران'

    def __str__(self):
        return f"{self.phone_number} ({self.get_status_display()})"

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