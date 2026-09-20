"""
فرم تسویه حساب.

قبلاً SubmitOrderView مستقیم از request.POST.get(...) می‌خواند و هیچ اعتبارسنجی‌ای نداشت:
سفارش با آدرس خالی، نام خالی یا شماره‌ی نامعتبر هم ثبت می‌شد و همان داده‌ی ناقص به فاکتور
هلو می‌رفت.
"""

from django import forms

from accounts.models import normalize_phone_number
from products.pricing import VALID_PAYMENT_METHODS


class CheckoutForm(forms.Form):
    # همه‌ی فیلدها در سطح فیلد required=False‌اند و الزامی بودنشان در clean_* خودمان چک
    # می‌شود. دلیل: اگر required=True باشد، جنگو پیش از رسیدن به clean_<field> روی مقدار
    # خالی خطا می‌دهد و «پر شدن از روی پروفایل کاربر» هرگز فرصت اجرا پیدا نمی‌کند.
    first_name = forms.CharField(label='نام گیرنده', max_length=50, required=False)
    last_name = forms.CharField(label='نام خانوادگی گیرنده', max_length=50, required=False)
    phone = forms.CharField(label='شماره تماس گیرنده', max_length=15, required=False)
    address = forms.CharField(label='آدرس کامل', widget=forms.Textarea, required=False)
    postal_code = forms.CharField(label='کد پستی', max_length=10, required=False)
    payment_method = forms.CharField(required=False)

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        # اگر کاربر فیلدی را خالی گذاشت، مقدار پیش‌فرضش (از آدرس پیش‌فرض کاربر؛ نام و موبایل در نبودِ آدرس
        # از پروفایل) جایگزین می‌شود، ولی این‌بار با اعتبارسنجی
        self.profile_defaults = self._build_defaults(user)
        for field, value in self.profile_defaults.items():
            self.fields[field].initial = value

    @staticmethod
    def _build_defaults(user):
        if user is None:
            return {}
        address = user.default_address
        return {
            'first_name': (address.receiver_first_name if address else '') or user.first_name or '',
            'last_name': (address.receiver_last_name if address else '') or user.last_name or '',
            'phone': (address.receiver_phone if address else '') or user.phone_number or '',
            'address': address.full_text if address else '',
            'postal_code': address.postal_code if address else '',
        }

    def _fallback(self, name):
        value = (self.data.get(name) or '').strip()
        return value or self.profile_defaults.get(name, '')

    def clean_first_name(self):
        value = self._fallback('first_name')
        if not value:
            raise forms.ValidationError('نام گیرنده الزامی است.')
        return value

    def clean_last_name(self):
        value = self._fallback('last_name')
        if not value:
            raise forms.ValidationError('نام خانوادگی گیرنده الزامی است.')
        return value

    def clean_address(self):
        value = self._fallback('address')
        if not value:
            raise forms.ValidationError('آدرس تحویل سفارش الزامی است.')
        return value

    def clean_phone(self):
        raw = self._fallback('phone')
        try:
            return normalize_phone_number(raw)
        except ValueError as e:
            raise forms.ValidationError(str(e))

    def clean_postal_code(self):
        value = self._fallback('postal_code')
        if value and (not value.isdigit() or len(value) != 10):
            raise forms.ValidationError('کد پستی باید ۱۰ رقم عددی باشد.')
        return value

    def clean_payment_method(self):
        # اعتبارسنجی نهایی (قفل کاربر ویژه و مقدار نامعتبر) در products.pricing انجام می‌شود؛
        # اینجا فقط مقادیر آشکارا بی‌ربط را دور می‌ریزیم
        value = (self.cleaned_data.get('payment_method') or '').strip()
        return value if value in VALID_PAYMENT_METHODS else ''

    @property
    def error_text(self):
        messages = []
        for field_errors in self.errors.values():
            messages.extend(field_errors)
        return ' '.join(messages)
