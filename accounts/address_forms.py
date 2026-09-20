"""
فرم ثبت/ویرایش آدرس (مشترک بین افزودن و ویرایش).

استان فیلد مدل نیست (استان از city.province خوانده می‌شود)؛ فقط برای فیلتر کردن کمبوی شهر است و
سمت سرور هم با شهر هماهنگ بودنش چک می‌شود. اعتبارسنجی اصلی (شهر/ناحیه، موبایل، کدپستی، الزامی‌ها)
همان Address.clean() است، پس فرم و ذخیره‌ی مستقیم از یک قاعده پیروی می‌کنند.
"""

from django import forms

from locations.models import City, DeliveryZone, Province

from .models import Address


def _to_int(value):
    return int(value) if value is not None and str(value).isdigit() else None


class AddressForm(forms.ModelForm):
    province = forms.ModelChoiceField(
        queryset=Province.objects.order_by('name'), label='استان', empty_label='انتخاب استان',
        error_messages={'required': 'انتخاب استان الزامی است.', 'invalid_choice': 'استان انتخاب‌شده معتبر نیست.'},
    )

    field_order = ('title', 'province', 'city', 'zone', 'address', 'postal_code',
                   'receiver_first_name', 'receiver_last_name', 'receiver_phone', 'is_default')

    class Meta:
        model = Address
        fields = ('title', 'receiver_first_name', 'receiver_last_name', 'receiver_phone',
                  'city', 'zone', 'postal_code', 'address', 'is_default')
        error_messages = {
            'title': {'required': 'عنوان آدرس الزامی است.'},
            'receiver_first_name': {'required': 'نام گیرنده الزامی است.'},
            'receiver_last_name': {'required': 'نام خانوادگی گیرنده الزامی است.'},
            'receiver_phone': {'required': 'موبایل گیرنده الزامی است.'},
            'postal_code': {'required': 'کد پستی الزامی است.'},
            'address': {'required': 'آدرس دقیق الزامی است.'},
            'city': {'required': 'انتخاب شهر الزامی است.', 'invalid_choice': 'شهر انتخاب‌شده معتبر نیست.'},
            'zone': {'invalid_choice': 'ناحیه‌ی انتخاب‌شده معتبر نیست.'},
        }

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.instance.user = user

        instance_city = self.instance.city if self.instance.city_id else None
        if self.is_bound:
            province_id = _to_int(self.data.get('province'))
            city_id = _to_int(self.data.get('city'))
        else:
            province_id = instance_city.province_id if instance_city else None
            city_id = instance_city.pk if instance_city else None
            if not self.instance.pk:                           # آدرس جدید: مشخصات گیرنده از پروفایل پر شود (قابل تغییر)
                # initial از مدل با مقدار خالی پر شده؛ setdefault اثری نداشت
                self.initial['receiver_first_name'] = user.first_name or ''
                self.initial['receiver_last_name'] = user.last_name or ''
                self.initial['receiver_phone'] = user.phone_number
            elif instance_city:
                self.initial['province'] = province_id

        # شهرها/ناحیه‌ها فقط از استان/شهرِ انتخاب‌شده (و فقط فعال‌ها)؛ بقیه اصلاً معتبر نیستند
        self.fields['city'].queryset = (City.objects.filter(province_id=province_id, is_active=True).order_by('name')
                                        if province_id else City.objects.none())
        self.fields['zone'].queryset = (DeliveryZone.objects.filter(city_id=city_id, is_active=True).order_by('sort_order', 'name')
                                        if city_id else DeliveryZone.objects.none())
        self.fields['city'].empty_label = 'انتخاب شهر'
        self.fields['zone'].empty_label = 'انتخاب ناحیه'

        self.selected_province_id = province_id
        self.selected_city_id = city_id
        self.selected_zone_id = (_to_int(self.data.get('zone')) if self.is_bound else self.instance.zone_id)

        # اولین آدرس کاربر همیشه پیش‌فرض می‌شود و آدرس پیش‌فرضِ فعلی را نمی‌شود مستقیم از حالت پیش‌فرض درآورد
        self.is_default_locked = (not user.addresses.exclude(pk=self.instance.pk).exists()) or bool(self.instance.pk and self.instance.is_default)

    def clean(self):
        cleaned = super().clean()
        province, city = cleaned.get('province'), cleaned.get('city')
        if province and city and city.province_id != province.pk:
            self.add_error('city', 'این شهر مربوط به استان انتخاب‌شده نیست.')
        return cleaned

    # ------------------------------------------------------------------ برای قالب
    @property
    def zone_choices(self):
        """ ناحیه‌های فعالِ شهر انتخاب‌شده (خالی = شهر ناحیه‌دار نیست) """
        return self.fields['zone'].queryset

    @property
    def zone_error(self):
        errors = self.errors.get('zone')
        return errors[0] if errors else ''
