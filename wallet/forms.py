"""
فرم درخواست برداشت. اعتبارسنجی مبلغ اینجا فقط یک چک سریع/دوستانه برای UX است (بر اساس
یک خواندن ساده‌ی wallet.available_balance)؛ چک واقعی و امن در برابر هم‌زمانی همیشه داخل
wallet.services.reserve_withdrawal (با select_for_update) انجام می‌شود - نگاه کنید
wallet/views.py:WalletWithdrawView.
"""

import re
from decimal import Decimal, InvalidOperation

from django import forms

_IBAN_DIGITS_RE = re.compile(r'^\d{24}$')
_CARD_DIGITS_RE = re.compile(r'^\d{16}$')


class WithdrawalRequestForm(forms.Form):
    amount = forms.CharField(label='مبلغ برداشت (تومان)')
    iban = forms.CharField(label='شماره شبا', max_length=30, help_text='با یا بدون پیشوند IR، ۲۴ رقم.')
    card_number = forms.CharField(label='شماره کارت (۱۶ رقم)', max_length=25)
    account_holder = forms.CharField(label='نام و نام‌خانوادگی صاحب حساب', max_length=100)

    def __init__(self, *args, wallet=None, **kwargs):
        self.wallet = wallet
        super().__init__(*args, **kwargs)

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

    def clean_iban(self):
        raw = (self.cleaned_data.get('iban') or '').strip().upper().replace(' ', '')
        digits = raw[2:] if raw.startswith('IR') else raw
        if not _IBAN_DIGITS_RE.match(digits):
            raise forms.ValidationError('شماره شبا باید ۲۴ رقم باشد (با یا بدون پیشوند IR).')
        return f'IR{digits}'

    def clean_card_number(self):
        digits = re.sub(r'\D', '', self.cleaned_data.get('card_number') or '')
        if not _CARD_DIGITS_RE.match(digits):
            raise forms.ValidationError('شماره کارت باید دقیقاً ۱۶ رقم باشد.')
        return digits

    def clean_account_holder(self):
        value = (self.cleaned_data.get('account_holder') or '').strip()
        if not value:
            raise forms.ValidationError('نام صاحب حساب الزامی است.')
        return value
