"""
مدل آدرس کاربر (چندآدرسی).

قواعد:
  - هر کاربر هر تعداد آدرس می‌تواند داشته باشد (سقف سخت‌کد نشده).
  - کاربر بدون آدرس، آدرس پیش‌فرض ندارد؛ کاربر با حداقل یک آدرس، *دقیقاً* یک آدرس پیش‌فرض دارد.
  - تمام منطق «پیش‌فرض» در save()/delete()/QuerySet.delete() است، پس ادمین، ویو، شل و تسک همه از
    یک مسیر رد می‌شوند. برای جلوگیری از رقابت بین درخواست‌های هم‌زمان، همه‌ی تغییرات داخل
    transaction.atomic و با قفل *ردیف کاربر* (select_for_update) انجام می‌شود؛ قفل روی ردیف کاربر
    است (نه روی آدرس‌ها) چون کاربری که هنوز آدرس ندارد ردیفی برای قفل کردن ندارد و دو درخواست
    هم‌زمانِ «اولین آدرس» هر دو خودشان را پیش‌فرض می‌کردند. ترتیب قفل همه‌جا یکی است (اول کاربر،
    بعد آدرس‌ها) تا بن‌بست (deadlock) پیش نیاید.
  - پشتیبان دیتابیسی: ایندکس یکتای فیلترشده (WHERE is_default = 1) روی user، تا اگر کدی قاعده را
    دور زد (مثلاً QuerySet.update) دیتابیس دو پیش‌فرض را رد کند.
  - اعتبارسنجی هماهنگی شهر/ناحیه فقط در فرم/clean() نیست؛ save() هم آن را اجرا می‌کند (جنگو
    clean() را خودکار صدا نمی‌زند).
"""

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .signals import default_address_changed


def _lock_user_rows(user_ids):
    """
    ردیف کاربرها را (به ترتیب pk) قفل می‌کند و باید داخل atomic صدا زده شود.
    عمداً .get()/.first() استفاده نشده: آن‌ها LIMIT می‌زنند و بک‌اند SQL Server از
    select_for_update همراه با LIMIT پشتیبانی نمی‌کند.
    """
    user_model = apps.get_model('accounts', 'CustomUser')
    return list(user_model.objects.select_for_update().filter(pk__in=list(user_ids)).order_by('pk')
                .values_list('pk', flat=True))


class AddressQuerySet(models.QuerySet):
    def delete(self):
        """
        حذف گروهی (مثلاً action ادمین): پیش از حذف، کاربرهای درگیر قفل می‌شوند (همان ترتیب save/delete)
        تا با ویرایش هم‌زمان بن‌بست نشود؛ بعد سیگنال post_delete برای هر کاربری که پیش‌فرضش پاک شده
        و هنوز آدرس دارد، یک آدرس دیگر را پیش‌فرض می‌کند.
        """
        user_ids = sorted(set(self.values_list('user_id', flat=True)))
        with transaction.atomic():
            _lock_user_rows(user_ids)
            return super().delete()


class Address(models.Model):
    user = models.ForeignKey('accounts.CustomUser', on_delete=models.CASCADE, related_name='addresses', verbose_name='کاربر')
    title = models.CharField(max_length=50, verbose_name='عنوان آدرس', help_text='مثال: منزل، محل کار')

    receiver_first_name = models.CharField(max_length=50, verbose_name='نام گیرنده')
    receiver_last_name = models.CharField(max_length=50, verbose_name='نام خانوادگی گیرنده')
    receiver_phone = models.CharField(max_length=11, verbose_name='موبایل گیرنده')

    # استان از روی شهر خوانده می‌شود (city.province)؛ ذخیره‌ی جدا اجازه‌ی ناسازگاری استان/شهر می‌داد
    city = models.ForeignKey('locations.City', on_delete=models.PROTECT, related_name='addresses', verbose_name='شهر')
    zone = models.ForeignKey(
        'locations.DeliveryZone', on_delete=models.PROTECT, null=True, blank=True, related_name='addresses',
        verbose_name='ناحیه', help_text='فقط برای شهرهایی که ناحیه‌ی ارسال فعال دارند و در آن‌ها اجباری است.',
    )
    postal_code = models.CharField(max_length=10, verbose_name='کد پستی')
    address = models.TextField(verbose_name='آدرس دقیق')

    is_default = models.BooleanField(default=False, verbose_name='آدرس پیش‌فرض')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='تاریخ ثبت')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='آخرین ویرایش')

    objects = AddressQuerySet.as_manager()

    class Meta:
        verbose_name = 'آدرس'
        verbose_name_plural = 'آدرس‌ها'
        ordering = ('-is_default', '-created_at', '-id')
        constraints = [
            # ایندکس یکتای فیلترشده: هر کاربر حداکثر یک ردیف is_default=True
            models.UniqueConstraint(fields=('user',), condition=Q(is_default=True), name='unique_default_address_per_user'),
        ]

    def __str__(self):
        return f'{self.title} - {self.receiver_first_name} {self.receiver_last_name}'

    # ------------------------------------------------------------------ خواندنی‌ها
    @property
    def province(self):
        return self.city.province

    @property
    def receiver_full_name(self):
        return f'{self.receiver_first_name} {self.receiver_last_name}'.strip()

    @property
    def full_text(self):
        """ «استان، شهر، ناحیه، آدرس» برای نمایش و ارسال به هلو/فاکتور """
        parts = [self.city.province.name, self.city.name, self.zone.name if self.zone_id else '', self.address]
        return '، '.join(p.strip() for p in parts if p and p.strip())

    # ------------------------------------------------------------------ اعتبارسنجی
    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        # موقعیت بارگذاری‌شده را نگه می‌داریم تا save() بفهمد شهر/ناحیه تغییر کرده یا نه
        instance._loaded_location = (instance.__dict__.get('city_id'), instance.__dict__.get('zone_id'))
        return instance

    def _validation_errors(self, *, strict):
        """
        دیکشنری خطاها را می‌سازد. strict=True (فرم/clean) همه‌ی قواعد را اعمال می‌کند؛ save() فقط
        وقتی آدرس تازه است یا شهر/ناحیه عوض شده strict عمل می‌کند تا ذخیره‌ی بی‌ربطی مثل «تنظیم
        پیش‌فرض» روی آدرس قدیمی (مثلاً آدرسِ منتقل‌شده از ساختار قبلی که هنوز ناحیه ندارد) خطا ندهد.
        """
        from accounts.models import normalize_phone_number

        errors = {}
        for name, label in (('title', 'عنوان آدرس'), ('receiver_first_name', 'نام گیرنده'),
                            ('receiver_last_name', 'نام خانوادگی گیرنده'), ('address', 'آدرس دقیق')):
            if not (getattr(self, name) or '').strip():
                errors[name] = f'{label} الزامی است.'

        try:
            self.receiver_phone = normalize_phone_number(self.receiver_phone)
        except ValueError as exc:
            errors['receiver_phone'] = str(exc)

        postal = (self.postal_code or '').strip()
        if not postal.isdigit() or len(postal) != 10:
            errors['postal_code'] = 'کد پستی باید ۱۰ رقم عددی باشد.'
        else:
            self.postal_code = postal

        if not self.city_id:
            errors['city'] = 'انتخاب شهر الزامی است.'
            return errors

        location_changed = self._state.adding or (self.city_id, self.zone_id) != getattr(self, '_loaded_location', None)
        enforce = strict or location_changed

        if self.zone_id:
            zone = self.zone
            if zone.city_id != self.city_id:
                errors['zone'] = 'ناحیه‌ی انتخاب‌شده مربوط به این شهر نیست.'
            elif enforce and not zone.is_active:
                errors['zone'] = 'این ناحیه‌ی ارسال فعال نیست.'
        elif enforce and self.city.active_zones().exists():
            errors['zone'] = 'برای این شهر انتخاب ناحیه الزامی است.'

        if enforce and not self.city.is_active:
            errors['city'] = 'این شهر فعال نیست.'
        return errors

    def clean(self):
        errors = self._validation_errors(strict=True)
        if errors:
            raise ValidationError(errors)

    # ------------------------------------------------------------------ ذخیره / حذف
    def save(self, *args, **kwargs):
        update_fields = kwargs.get('update_fields')
        # ذخیره‌ی فقط پرچم پیش‌فرض (set_default) محتوای آدرس را عوض نمی‌کند؛ اعتبارسنجی نمی‌شود تا
        # آدرسِ ناقصِ منتقل‌شده از ساختار قبلی (مثلاً بدون کدپستی) قابل پیش‌فرض‌شدن بماند
        if update_fields is None or not set(update_fields) <= {'is_default', 'updated_at'}:
            errors = self._validation_errors(strict=False)
            if errors:
                raise ValidationError(errors)

        with transaction.atomic():
            _lock_user_rows([self.user_id])
            siblings = Address.objects.filter(user_id=self.user_id)
            if self.pk:
                siblings = siblings.exclude(pk=self.pk)

            wanted_default = self.is_default
            if not siblings.exists():
                self.is_default = True                      # اولین آدرس کاربر همیشه پیش‌فرض است
            elif self.is_default:
                siblings.filter(is_default=True).update(is_default=False)   # قبلی باید *پیش از* ذخیره‌ی این یکی خاموش شود
            elif not siblings.filter(is_default=True).exists():
                self.is_default = True                      # پیش‌فرضِ کاربر نباید بدون جانشین خاموش شود

            if update_fields is not None and self.is_default != wanted_default:
                kwargs['update_fields'] = set(update_fields) | {'is_default'}

            super().save(*args, **kwargs)
            self._loaded_location = (self.city_id, self.zone_id)

            if self.is_default:
                user = self.user
                transaction.on_commit(lambda: default_address_changed.send_robust(sender=Address, user=user, address=self))

    def refresh_from_db(self, *args, **kwargs):
        super().refresh_from_db(*args, **kwargs)
        self._loaded_location = (self.city_id, self.zone_id)

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            _lock_user_rows([self.user_id])
            # شیء در حافظه ممکن است کهنه باشد (مثلاً بعد از جانشین‌شدنِ خودکار در حذف قبلی پیش‌فرض شده
            # ولی این نمونه هنوز False دارد)؛ post_delete باید وضعیت واقعیِ دیتابیس را ببیند
            current = list(Address.objects.filter(pk=self.pk).values_list('is_default', flat=True))
            if current:
                self.is_default = current[0]
            return super().delete(*args, **kwargs)

    def set_default(self):
        """ این آدرس را پیش‌فرض می‌کند (بقیه‌ی آدرس‌های کاربر خودکار خاموش می‌شوند) """
        self.is_default = True
        self.save(update_fields=['is_default', 'updated_at'])


@receiver(post_delete, sender=Address, dispatch_uid='accounts_address_promote_default')
def promote_default_after_delete(sender, instance, **kwargs):
    """
    اگر آدرس پیش‌فرضِ کاربری پاک شد و آدرس دیگری دارد، جدیدترین آدرس پیش‌فرض می‌شود.
    (چون روی سیگنال است، حذف گروهی ادمین و حذف تکی هر دو را پوشش می‌دهد.)
    """
    if not instance.is_default:
        return
    with transaction.atomic():
        if not _lock_user_rows([instance.user_id]):
            return   # خودِ کاربر پاک شده (cascade)؛ چیزی برای جانشینی نیست
        remaining = Address.objects.filter(user_id=instance.user_id)
        if remaining.filter(is_default=True).exists():
            return
        successor = remaining.order_by('-created_at', '-id').first()
        if successor is None:
            return
        Address.objects.filter(pk=successor.pk).update(is_default=True)
        successor.is_default = True
        user = successor.user
        transaction.on_commit(lambda: default_address_changed.send_robust(sender=Address, user=user, address=successor))
