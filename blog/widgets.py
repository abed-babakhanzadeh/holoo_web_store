import jdatetime
from django import forms
from django.utils import timezone


class JalaliDateTimeWidget(forms.MultiWidget):
    """ ویجت تاریخ‌وساعت شمسی برای ادمین: زیرویجت اول تاریخ (با همان انتخابگر JS بدون
    وابستگی خارجی که در پروفایل کاربر استفاده می‌شود)، زیرویجت دوم ساعت (input نیتیو مرورگر).

    template_name سفارشی: پیکر js پنل تقویم را با insertAdjacentElement('afterend', ...)
    درست بعد از input تاریخ اضافه می‌کند و خود پنل position:absolute دارد. بدون یک جدِ
    positioned دور همان input (که در پروفایل کاربر با یک div.relative تأمین شده)، این
    absolute نسبت به viewport حساب می‌شود و پنل به‌جای باز شدن کنار باکس، پایین/گوشه‌ی
    صفحه ظاهر می‌شود؛ چون MultiWidget زیرویجت‌ها را با template رندر می‌کند نه با فراخوانی
    render() هرکدام، تنها راه اضافه‌کردن این wrapper یک template اختصاصی است. """

    template_name = 'blog/widgets/jalali_datetime.html'

    class Media:
        js = ('theme/assets/js/jalali-datepicker.js',)

    def __init__(self, attrs=None):
        widgets = [
            forms.TextInput(attrs={
                'data-jalali-datepicker': '1', 'autocomplete': 'off', 'readonly': 'readonly',
                'placeholder': '۱۴۰۴/۰۱/۰۱', 'style': 'width: 8em; cursor: pointer; background: #fff;',
                'title': 'برای انتخاب تاریخ کلیک کنید',
            }),
            forms.TimeInput(attrs={'type': 'time'}, format='%H:%M'),
        ]
        super().__init__(widgets, attrs)

    def decompress(self, value):
        if not value:
            return [None, None]
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        jd = jdatetime.datetime.fromgregorian(datetime=value)
        return [jd.strftime('%Y/%m/%d'), jd.strftime('%H:%M')]


class JalaliSplitDateTimeField(forms.MultiValueField):
    """ فیلد فرم متناظر با JalaliDateTimeWidget؛ مقدار نهایی را به datetime میلادی تبدیل می‌کند """

    def __init__(self, *args, **kwargs):
        fields = (forms.CharField(required=False), forms.CharField(required=False))
        # ModelAdmin.formfield_overrides برای DateTimeField مقدار پیش‌فرض جنگو (شامل
        # widget=AdminSplitDateTime) را با override ما merge می‌کند، نه جایگزین؛ پس widget
        # باید همیشه صراحتاً همین باشد، نه setdefault که با کلید از پیش موجود کاری نمی‌کند
        kwargs['widget'] = JalaliDateTimeWidget()
        kwargs['require_all_fields'] = False
        super().__init__(fields=fields, *args, **kwargs)

    def compress(self, data_list):
        if not data_list:
            return None
        date_str, time_str = data_list
        date_str = (date_str or '').strip()
        if not date_str:
            return None
        time_str = (time_str or '00:00').strip()
        try:
            year, month, day = (int(p) for p in date_str.split('/'))
            hour, minute = (int(p) for p in time_str.split(':')[:2])
            jd = jdatetime.datetime(year, month, day, hour, minute)
        except (ValueError, TypeError):
            raise forms.ValidationError('فرمت تاریخ نامعتبر است؛ مثال: ۱۴۰۴/۰۱/۰۱')
        gregorian = jd.togregorian()
        return timezone.make_aware(gregorian) if timezone.is_naive(gregorian) else gregorian
