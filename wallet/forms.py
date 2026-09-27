"""
فرم درخواست برداشت. اعتبارسنجی مبلغ اینجا فقط یک چک سریع/دوستانه برای UX است (بر اساس
یک خواندن ساده‌ی wallet.available_balance)؛ چک واقعی و امن در برابر هم‌زمانی همیشه داخل
wallet.services.reserve_withdrawal (با select_for_update) انجام می‌شود - نگاه کنید
wallet/views.py:WalletWithdrawView.
"""

import re
from decimal import Decimal, InvalidOperation

from django import forms

from accounts.models import UserBankAccount

_IBAN_DIGITS_RE = re.compile(r'^\d{24}$')
_CARD_DIGITS_RE = re.compile(r'^\d{16}$')

NEW_ACCOUNT_CHOICE = 'new'


class WithdrawalRequestForm(forms.Form):
    amount = forms.CharField(label='مبلغ برداشت (تومان)')
    bank_account = forms.ChoiceField(label='حساب مقصد', required=False)
    iban = forms.CharField(label='شماره شبا', max_length=30, required=False, help_text='با یا بدون پیشوند IR، ۲۴ رقم.')
    card_number = forms.CharField(label='شماره کارت (۱۶ رقم)', max_length=25, required=False)
    account_holder = forms.CharField(label='نام و نام‌خانوادگی صاحب حساب', max_length=100, required=False)

    def __init__(self, *args, wallet=None, **kwargs):
        self.wallet = wallet
        super().__init__(*args, **kwargs)
        # کمبوباکس فقط حساب‌های همین کاربر را نشان می‌دهد + گزینه‌ی «حساب جدید» برای ورود دستی
        self._saved_accounts = UserBankAccount.objects.filter(user=wallet.user) if wallet else UserBankAccount.objects.none()
        choices = [(str(acc.pk), acc.masked_display) for acc in self._saved_accounts]
        choices.append((NEW_ACCOUNT_CHOICE, 'وارد کردن حساب جدید'))
        self.fields['bank_account'].choices = choices
        default_account = next((acc for acc in self._saved_accounts if acc.is_default), None)
        self.fields['bank_account'].initial = str(default_account.pk) if default_account else NEW_ACCOUNT_CHOICE

    def clean_amount(self):
        raw = (self.cleaned_data.get('amount') or '').replace(',', '').strip()
        try:
            amount = Decimal(raw)
        except (InvalidOperation, ValueError):
            raise forms.ValidationError('مبلغ واردشده نامعتبر است.')
        if amount <= 0:
            raise forms.ValidationError('مبلغ باید بزرگ‌تر از صفر باشد.')
        if self.wallet is not None and amount > self.wallet.available_balance:
            raise forms.ValidationError('مبلغ درخواستی از موجودی قابل‌استفاده‌ی شما بیشتر است.')
        return amount

    def _using_saved_account(self):
        selected = self.cleaned_data.get('bank_account')
        return bool(selected) and selected != NEW_ACCOUNT_CHOICE

    def clean_iban(self):
        if self._using_saved_account():
            return ''   # از حساب ذخیره‌شده خوانده می‌شود - نگاه کنید clean()
        raw = (self.cleaned_data.get('iban') or '').strip().upper().replace(' ', '')
        digits = raw[2:] if raw.startswith('IR') else raw
        if not _IBAN_DIGITS_RE.match(digits):
            raise forms.ValidationError('شماره شبا باید ۲۴ رقم باشد (با یا بدون پیشوند IR).')
        return f'IR{digits}'

    def clean_card_number(self):
        if self._using_saved_account():
            return ''
        digits = re.sub(r'\D', '', self.cleaned_data.get('card_number') or '')
        if not _CARD_DIGITS_RE.match(digits):
            raise forms.ValidationError('شماره کارت باید دقیقاً ۱۶ رقم باشد.')
        return digits

    def clean_account_holder(self):
        if self._using_saved_account():
            return ''
        value = (self.cleaned_data.get('account_holder') or '').strip()
        if not value:
            raise forms.ValidationError('نام صاحب حساب الزامی است.')
        return value

    def clean(self):
        cleaned = super().clean()
        selected = cleaned.get('bank_account')
        if selected and selected != NEW_ACCOUNT_CHOICE:
            account = next((acc for acc in self._saved_accounts if str(acc.pk) == selected), None)
            if account is None:
                raise forms.ValidationError('حساب انتخاب‌شده نامعتبر است.')
            self.selected_bank_account = account
        else:
            self.selected_bank_account = None
        return cleaned

    def resolve_bank_account(self):
        """
        حساب نهایی که باید در ReturnRequest/WithdrawalRequest استفاده شود: یا همان حساب
        ذخیره‌شده‌ی انتخاب‌شده، یا یک UserBankAccount تازه که از فیلدهای دستی ساخته و برای
        دفعات بعد ذخیره می‌شود (اولین حساب کاربر خودکار پیش‌فرض می‌شود - نگاه کنید
        UserBankAccount.save()).
        """
        if self.selected_bank_account is not None:
            return self.selected_bank_account
        full_name = self.cleaned_data['account_holder']
        parts = full_name.split(None, 1)
        first_name, last_name = (parts[0], parts[1]) if len(parts) == 2 else (full_name, full_name)
        account, _created = UserBankAccount.objects.get_or_create(
            user=self.wallet.user, card_number=self.cleaned_data['card_number'], iban=self.cleaned_data['iban'],
            defaults={'account_holder_first_name': first_name, 'account_holder_last_name': last_name},
        )
        return account
