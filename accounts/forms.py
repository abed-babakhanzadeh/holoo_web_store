"""
فرم‌های حساب کاربری.

قبلاً اعتبارسنجی پروفایل در دو ویو (ProfileCompleteView و ProfileView) تقریباً عیناً کپی
شده بود: همان چک ۱۰ رقمی کد ملی، همان چک کد پستی، همان بررسی پر بودن فیلدها. حالا قوانین
یک‌جا تعریف می‌شوند و هر دو ویو از همان استفاده می‌کنند.
"""

import jdatetime
from django import forms
from django.contrib.auth.password_validation import validate_password

from .models import CustomUser


class TenDigitField(forms.CharField):
    """ کد ملی / کد پستی: دقیقاً ۱۰ رقم """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('max_length', 10)
        super().__init__(*args, **kwargs)

    def clean(self, value):
        value = (super().clean(value) or '').strip()
        if value and (not value.isdigit() or len(value) != 10):
            raise forms.ValidationError(f'{self.label} باید ۱۰ رقم عددی باشد.')
        return value


class BaseProfileForm(forms.ModelForm):
    """ فیلدهای مشترک پروفایل بین «تکمیل اطلاعات» و «ویرایش پروفایل» """

    national_code = TenDigitField(label='کد ملی', required=True)

    class Meta:
        model = CustomUser
        # آدرس جزو پروفایل نیست (مدل Address، از صفحه‌ی «آدرس‌ها» یا هنگام تسویه‌حساب)
        fields = ('first_name', 'last_name', 'national_code')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ('first_name', 'last_name'):
            self.fields[name].required = True

    def clean(self):
        cleaned = super().clean()
        for name, value in cleaned.items():
            if isinstance(value, str):
                cleaned[name] = value.strip()
        return cleaned

    @property
    def error_text(self):
        """ همه‌ی خطاها در یک رشته، برای قالب‌های فعلی که فقط یک {{ error }} نمایش می‌دهند """
        messages = []
        for field_errors in self.errors.values():
            messages.extend(field_errors)
        return ' '.join(messages)


class ProfileCompleteForm(BaseProfileForm):
    """ گام تکمیل اطلاعات پس از اولین ورود؛ تعیین رمز عبور هم اینجا الزامی است """

    password = forms.CharField(label='رمز عبور', widget=forms.PasswordInput, required=True)
    confirm_password = forms.CharField(label='تکرار رمز عبور', widget=forms.PasswordInput, required=True)

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get('password')
        confirm = cleaned.get('confirm_password')

        if password and confirm and password != confirm:
            self.add_error('confirm_password', 'رمز عبور و تکرار آن یکسان نیستند.')
        elif password:
            try:
                validate_password(password, self.instance)
            except forms.ValidationError as e:
                self.add_error('password', e)
        return cleaned


class ProfileEditForm(BaseProfileForm):
    """ ویرایش پروفایل در پنل کاربری؛ ایمیل و تاریخ تولد شمسی اختیاری‌اند """

    email = forms.EmailField(label='پست الکترونیک', required=False)
    # ورودی قالب تاریخ شمسی می‌فرستد (نام فیلد در فرم HTML: birth_date)
    birth_date = forms.CharField(label='تاریخ تولد', required=False)

    class Meta(BaseProfileForm.Meta):
        fields = BaseProfileForm.Meta.fields + ('email',)

    def clean_email(self):
        # مدل روی ایمیل خالی None می‌خواهد نه رشته‌ی خالی
        return (self.cleaned_data.get('email') or '').strip() or None

    def clean_birth_date(self):
        raw = (self.cleaned_data.get('birth_date') or '').strip()
        if not raw:
            return None
        try:
            return jdatetime.datetime.strptime(raw, '%Y/%m/%d').togregorian().date()
        except ValueError:
            raise forms.ValidationError('قالب تاریخ تولد نامعتبر است.')

    def save(self, commit=True):
        user = super().save(commit=False)
        user.birth_date = self.cleaned_data.get('birth_date')
        if commit:
            user.save()
        return user


class ChangePasswordForm(forms.Form):
    """ تغییر رمز عبور در پنل کاربری (رمز فعلی فقط وقتی لازم است که کاربر از قبل رمز داشته باشد) """

    current_password = forms.CharField(label='رمز عبور فعلی', widget=forms.PasswordInput, required=False)
    new_password = forms.CharField(label='رمز عبور جدید', widget=forms.PasswordInput)
    confirm_password = forms.CharField(label='تکرار رمز عبور جدید', widget=forms.PasswordInput)

    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.has_password = user.has_real_password()
        self.fields['current_password'].required = self.has_password

    def clean_current_password(self):
        current = self.cleaned_data.get('current_password')
        if self.has_password and not self.user.check_password(current):
            raise forms.ValidationError('رمز عبور فعلی اشتباه است.')
        return current

    def clean(self):
        cleaned = super().clean()
        new = cleaned.get('new_password')
        confirm = cleaned.get('confirm_password')

        if new and confirm and new != confirm:
            self.add_error('confirm_password', 'رمز عبور جدید و تکرار آن یکسان نیستند.')
        elif new:
            try:
                validate_password(new, self.user)
            except forms.ValidationError as e:
                self.add_error('new_password', e)
        return cleaned

    @property
    def error_text(self):
        messages = []
        for field_errors in self.errors.values():
            messages.extend(field_errors)
        return ' '.join(messages)

    def save(self):
        self.user.set_password(self.cleaned_data['new_password'])
        self.user.save(update_fields=['password'])
        return self.user
