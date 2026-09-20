"""
تقسیمات کشوری و نواحی ارسال.

این اپ به هیچ اپ دیگری وابسته نیست (در سطح accounts/products «بنیادی» است): accounts.Address به
City و DeliveryZone وصل می‌شود و orders در فاز بعد از همین‌ها برای محاسبه‌ی کرایه استفاده می‌کند.
هیچ نامی (مثل «قم») در کد قفل نشده؛ اینکه یک شهر «ناحیه‌دار» است فقط از وجود DeliveryZone فعال
برای آن شهر در دیتابیس فهمیده می‌شود.
"""

from django.db import models


class Province(models.Model):
    name = models.CharField(max_length=100, unique=True, verbose_name='نام استان')
    sort_order = models.PositiveSmallIntegerField(default=0, verbose_name='ترتیب نمایش')

    class Meta:
        verbose_name = 'استان'
        verbose_name_plural = 'استان‌ها'
        ordering = ('sort_order', 'name')

    def __str__(self):
        return self.name


class City(models.Model):
    province = models.ForeignKey(Province, on_delete=models.PROTECT, related_name='cities', verbose_name='استان')
    name = models.CharField(max_length=100, verbose_name='نام شهر')
    is_active = models.BooleanField(
        default=True, verbose_name='فعال',
        help_text='شهر غیرفعال در فرم ثبت آدرس نمایش داده نمی‌شود (آدرس‌های قبلی دست نمی‌خورند).',
    )

    class Meta:
        verbose_name = 'شهر'
        verbose_name_plural = 'شهرها'
        ordering = ('province__name', 'name')
        constraints = [
            models.UniqueConstraint(fields=('province', 'name'), name='unique_city_name_per_province'),
        ]

    def __str__(self):
        return f'{self.name} ({self.province.name})'

    def active_zones(self):
        return self.zones.filter(is_active=True)

    @property
    def has_zones(self):
        """ شهر «ناحیه‌دار» است اگر حداقل یک ناحیه‌ی فعال داشته باشد (در فرم آدرس، انتخاب ناحیه اجباری می‌شود) """
        return self.active_zones().exists()


class DeliveryZone(models.Model):
    city = models.ForeignKey(City, on_delete=models.PROTECT, related_name='zones', verbose_name='شهر')
    name = models.CharField(max_length=100, verbose_name='نام ناحیه')
    shipping_cost = models.PositiveIntegerField(
        default=0, verbose_name='کرایه‌ی پیک (تومان)',
        help_text='۰ یعنی «تعرفه تنظیم‌نشده»؛ تا وقتی عدد بیشتر از صفر نگذاشته‌اید این ناحیه در هیچ محاسبه‌ای '
                  'شرکت نمی‌کند.',
    )
    is_active = models.BooleanField(default=True, verbose_name='فعال')
    sort_order = models.PositiveSmallIntegerField(default=0, verbose_name='ترتیب نمایش')

    class Meta:
        verbose_name = 'ناحیه‌ی ارسال'
        verbose_name_plural = 'نواحی ارسال'
        ordering = ('city__name', 'sort_order', 'name')
        constraints = [
            models.UniqueConstraint(fields=('city', 'name'), name='unique_zone_name_per_city'),
        ]

    def __str__(self):
        return f'{self.name} - {self.city.name}'

    @property
    def has_tariff(self):
        """ آیا برای این ناحیه تعرفه‌ی واقعی ثبت شده است (۰ = تعرفه تنظیم‌نشده) """
        return self.shipping_cost > 0
