"""
ولیدیتورهای رمز عبور با پیام‌های کاملاً فارسی. جایگزین MinimumLengthValidator/NumericPasswordValidator
پیش‌فرض جنگو شده‌اند چون کاتالوگ ترجمه‌ی فارسی خودِ جنگو پیام MinimumLengthValidator را ندارد (به
انگلیسی می‌ماند) و NumericPasswordValidator با وجود ContainsLetterValidator عملاً زائد است.

قوانین اینجا باید دقیقاً با چک‌های زنده‌ی سمت کاربر در static/theme/assets/js/password-rules.js
یکی باشند (طول، عدد، حرف، نماد).
"""
from django.core.exceptions import ValidationError

_PERSIAN_DIGITS = str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹')


class MinLengthValidator:
    def __init__(self, min_length=8):
        self.min_length = min_length

    def _message(self):
        return f'رمز عبور باید حداقل {str(self.min_length).translate(_PERSIAN_DIGITS)} کاراکتر باشد.'

    def validate(self, password, user=None):
        if len(password) < self.min_length:
            raise ValidationError(self._message(), code='password_too_short')

    def get_help_text(self):
        return self._message()


class ContainsLetterValidator:
    def validate(self, password, user=None):
        if not any(ch.isalpha() for ch in password):
            raise ValidationError('رمز عبور باید حداقل شامل یک حرف باشد.', code='password_no_letter')

    def get_help_text(self):
        return 'رمز عبور باید حداقل شامل یک حرف باشد.'


class ContainsDigitValidator:
    def validate(self, password, user=None):
        if not any(ch.isdigit() for ch in password):
            raise ValidationError('رمز عبور باید حداقل شامل یک عدد باشد.', code='password_no_digit')

    def get_help_text(self):
        return 'رمز عبور باید حداقل شامل یک عدد باشد.'


class ContainsSymbolValidator:
    def validate(self, password, user=None):
        if not any(not ch.isalnum() and not ch.isspace() for ch in password):
            raise ValidationError(
                'رمز عبور باید حداقل شامل یک نماد (مانند ! @ # $ %) باشد.', code='password_no_symbol'
            )

    def get_help_text(self):
        return 'رمز عبور باید حداقل شامل یک نماد (مانند ! @ # $ %) باشد.'
