"""
فرم‌های ویزارد سه‌مرحله‌ای مرجوعی کالا (Phase 1 - Part C.1).

هیچ فرمی این‌جا مستقیم رکورد نمی‌سازد یا مبلغ حساب نمی‌کند - فقط داده‌ی خام کاربر را پاک و
اعتبارسنجی می‌کند؛ ساخت واقعیِ ReturnRequest همیشه در returns/views.py با یک‌بار صدا زدن
services.create_return_request انجام می‌شود.
"""

import re

from django import forms

from accounts.models import UserBankAccount
from products.models import SiteSettings

from .models import ReturnAttachment, ReturnReason
from .refund_calculator import get_returnable_quantity

NEW_ACCOUNT_CHOICE = 'new'
_IBAN_DIGITS_RE = re.compile(r'^\d{24}$')
_CARD_DIGITS_RE = re.compile(r'^\d{16}$')

# فقط کلاس‌های ظاهری Tailwind هم‌سو با بقیه‌ی فرم‌های پروژه (نگاه کنید templates/wallet/withdraw_form.html)
_INPUT_CSS = ('w-full px-4 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary '
              'focus:border-transparent dark:bg-gray-700 dark:border-gray-600 dark:text-white')
_NUMBER_INPUT_CSS = ('w-24 px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary '
                     'focus:border-transparent dark:bg-gray-700 dark:border-gray-600 dark:text-white')

# پسوندهای مجاز مدارک مرجوعی (Part C.3) - همان فهرست FileExtensionValidator روی
# ReturnAttachment.file؛ این‌جا هم برای تفکیک نوع (عکس/فیلم) و هم پیام خطای زودهنگام لازم است
_ALLOWED_IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp'}
_ALLOWED_VIDEO_EXTENSIONS = {'mp4', 'mov', 'webm'}


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True

    def value_from_datadict(self, data, files, name):
        upload = files.getlist(name)
        return upload if upload else None


class MultipleFileField(forms.FileField):
    """ الگوی رسمی جنگو برای فیلد چندفایلی (django doc: "Uploading multiple files") """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('widget', MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single_file_clean = super().clean
        if isinstance(data, (list, tuple)):
            return [single_file_clean(f, initial) for f in data]
        return single_file_clean(data, initial) if data else []


class ReturnStepOneForm(forms.Form):
    """
    یک فیلد عددی به‌ازای هر OrderItem سفارش؛ سقفِ هرکدام get_returnable_quantity همان قلم است
    (نه فقط requested/quantity خام) تا کاربر حتی در فرم هم نتواند بیش از ظرفیت باقیمانده انتخاب
    کند - چک نهایی/امن باز هم داخل services.create_return_request تکرار می‌شود.
    """

    def __init__(self, *args, order, **kwargs):
        super().__init__(*args, **kwargs)
        self.order = order
        self.order_items = list(order.items.select_related('product'))
        self.returnable_quantities = {}
        for item in self.order_items:
            max_qty = get_returnable_quantity(item)
            self.returnable_quantities[item.pk] = max_qty
            self.fields[f'quantity_{item.pk}'] = forms.IntegerField(
                label=str(item.product) if item.product_id else f'قلم #{item.pk}',
                required=False, min_value=0, max_value=max_qty, initial=0,
                help_text=f'حداکثر قابل مرجوع: {max_qty} از {item.quantity}',
                widget=forms.NumberInput(attrs={'class': _NUMBER_INPUT_CSS}),
            )

    def clean(self):
        cleaned = super().clean()
        if any(not (0 <= (cleaned.get(f'quantity_{item.pk}') or 0) <= self.returnable_quantities[item.pk])
               for item in self.order_items):
            # min_value/max_value خودِ فیلد قبلاً این را رد کرده؛ این فقط یک محافظ اضافه است
            raise forms.ValidationError('یکی از مقادیر انتخاب‌شده نامعتبر است.')
        if not any((cleaned.get(f'quantity_{item.pk}') or 0) > 0 for item in self.order_items):
            raise forms.ValidationError('باید حداقل یک قلم را برای مرجوعی انتخاب کنید و تعداد آن را مشخص کنید.')
        return cleaned

    def selected_quantities(self):
        """ {order_item_id: quantity} فقط برای اقلامی که quantity > 0 انتخاب شده‌اند """
        result = {}
        for item in self.order_items:
            qty = self.cleaned_data.get(f'quantity_{item.pk}') or 0
            if qty > 0:
                result[item.pk] = qty
        return result


class ReturnStepTwoForm(forms.Form):
    """ به‌ازای هر OrderItem انتخاب‌شده در گام یک: دلیل مرجوعی + توضیح (اجباری اگر دلیل بخواهد) """

    def __init__(self, *args, order_items, **kwargs):
        super().__init__(*args, **kwargs)
        self.order_items = order_items
        reasons = ReturnReason.objects.filter(is_active=True)
        for item in order_items:
            self.fields[f'reason_{item.pk}'] = forms.ModelChoiceField(
                queryset=reasons, label=f'دلیل مرجوعی - {item.product if item.product_id else item.pk}',
                widget=forms.Select(attrs={'class': _INPUT_CSS}),
            )
            self.fields[f'description_{item.pk}'] = forms.CharField(
                label='توضیح', required=False,
                widget=forms.Textarea(attrs={'rows': 2, 'class': _INPUT_CSS}),
            )
            self.fields[f'attachments_{item.pk}'] = MultipleFileField(
                label='مدارک (عکس/فیلم)', required=False,
                widget=MultipleFileInput(attrs={
                    'multiple': True, 'class': 'sr-only', 'id': f'id_attachments_{item.pk}',
                    'accept': '.jpg,.jpeg,.png,.webp,.mp4,.mov,.webm',
                }),
            )

    def clean(self):
        cleaned = super().clean()
        settings_obj = SiteSettings.cached()
        for item in self.order_items:
            reason = cleaned.get(f'reason_{item.pk}')
            description = (cleaned.get(f'description_{item.pk}') or '').strip()
            if reason is not None and reason.requires_description and not description:
                self.add_error(f'description_{item.pk}', 'برای این دلیل، نوشتن توضیح الزامی است.')
            cleaned[f'description_{item.pk}'] = description
            cleaned[f'attachments_{item.pk}'] = self._clean_attachments(item, cleaned, settings_obj)
        return cleaned

    def _clean_attachments(self, item, cleaned, settings_obj):
        field_name = f'attachments_{item.pk}'
        files = cleaned.get(field_name) or []
        if len(files) > ReturnAttachment.MAX_PER_ITEM:
            self.add_error(field_name, f'حداکثر {ReturnAttachment.MAX_PER_ITEM} فایل برای این قلم مجاز است.')
            return []

        validated = []
        for uploaded in files:
            name = uploaded.name or ''
            extension = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
            if extension in _ALLOWED_IMAGE_EXTENSIONS:
                attachment_type = ReturnAttachment.IMAGE
                max_mb = settings_obj.return_attachment_max_image_mb
            elif extension in _ALLOWED_VIDEO_EXTENSIONS:
                attachment_type = ReturnAttachment.VIDEO
                max_mb = settings_obj.return_attachment_max_video_mb
            else:
                self.add_error(field_name, f'فرمت «{name}» مجاز نیست (فقط عکس jpg/jpeg/png/webp یا فیلم mp4/mov/webm).')
                continue
            if uploaded.size > max_mb * 1024 * 1024:
                self.add_error(field_name, f'حجم «{name}» بیشتر از سقف مجاز ({max_mb} مگابایت) است.')
                continue
            validated.append({'file': uploaded, 'attachment_type': attachment_type, 'original_filename': name})
        return validated

    def reasons_and_descriptions(self):
        """ {order_item_id: {'reason': ReturnReason, 'description': str}} """
        result = {}
        for item in self.order_items:
            result[item.pk] = {
                'reason': self.cleaned_data[f'reason_{item.pk}'],
                'description': self.cleaned_data.get(f'description_{item.pk}', ''),
            }
        return result

    def attachments_by_item(self):
        """ {order_item_id: [{'file', 'attachment_type', 'original_filename'}, ...]} - فقط فایل‌های معتبرشده """
        return {item.pk: self.cleaned_data.get(f'attachments_{item.pk}', []) for item in self.order_items}


class ReturnStepThreeForm(forms.Form):
    """
    انتخاب روش بازپرداخت. برای «bank»: کمبوباکس حساب‌های ذخیره‌شده‌ی کاربر + گزینه‌ی «حساب
    جدید» - دقیقاً هم‌الگوی wallet.forms.WithdrawalRequestForm (که خودش هم‌الگوی
    accounts.bank_account.UserBankAccount._validation_errors است).
    """
    refund_method = forms.ChoiceField(
        label='روش بازپرداخت', choices=(('wallet', 'شارژ کیف‌پول'), ('bank', 'واریز به حساب بانکی')),
        widget=forms.RadioSelect(attrs={'class': 'return-refund-method-radio'}),
    )
    bank_account = forms.ChoiceField(
        label='حساب مقصد', required=False,
        widget=forms.Select(attrs={'class': _INPUT_CSS, 'id': 'return-bank-account-select'}),
    )
    iban = forms.CharField(
        label='شماره شبا', max_length=30, required=False,
        widget=forms.TextInput(attrs={'class': _INPUT_CSS, 'dir': 'ltr'}),
    )
    card_number = forms.CharField(
        label='شماره کارت', max_length=25, required=False,
        widget=forms.TextInput(attrs={'class': _INPUT_CSS, 'dir': 'ltr'}),
    )
    account_holder = forms.CharField(
        label='نام و نام‌خانوادگی صاحب حساب', max_length=100, required=False,
        widget=forms.TextInput(attrs={'class': _INPUT_CSS}),
    )

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self._saved_accounts = list(UserBankAccount.objects.filter(user=user))
        choices = [(str(acc.pk), acc.masked_display) for acc in self._saved_accounts]
        choices.append((NEW_ACCOUNT_CHOICE, 'وارد کردن حساب جدید'))
        self.fields['bank_account'].choices = choices
        default_account = next((acc for acc in self._saved_accounts if acc.is_default), None)
        self.fields['bank_account'].initial = str(default_account.pk) if default_account else NEW_ACCOUNT_CHOICE

    def _using_saved_account(self):
        selected = self.cleaned_data.get('bank_account')
        return bool(selected) and selected != NEW_ACCOUNT_CHOICE

    def clean_iban(self):
        if self.cleaned_data.get('refund_method') != 'bank' or self._using_saved_account():
            return ''
        raw = (self.cleaned_data.get('iban') or '').strip().upper().replace(' ', '')
        if not raw:
            return ''   # ممکن است کاربر فقط کارت وارد کند؛ clean() سطح فرم حداقل یکی را می‌خواهد
        digits = raw[2:] if raw.startswith('IR') else raw
        if not _IBAN_DIGITS_RE.match(digits):
            raise forms.ValidationError('شماره شبا باید ۲۴ رقم باشد (با یا بدون پیشوند IR).')
        return f'IR{digits}'

    def clean_card_number(self):
        if self.cleaned_data.get('refund_method') != 'bank' or self._using_saved_account():
            return ''
        raw = (self.cleaned_data.get('card_number') or '').strip()
        if not raw:
            return ''
        digits = re.sub(r'\D', '', raw)
        if not _CARD_DIGITS_RE.match(digits):
            raise forms.ValidationError('شماره کارت باید دقیقاً ۱۶ رقم باشد.')
        return digits

    def clean_account_holder(self):
        if self.cleaned_data.get('refund_method') != 'bank' or self._using_saved_account():
            return ''
        return (self.cleaned_data.get('account_holder') or '').strip()

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('refund_method') != 'bank':
            self.selected_bank_account = None
            return cleaned

        selected = cleaned.get('bank_account')
        if selected and selected != NEW_ACCOUNT_CHOICE:
            account = next((acc for acc in self._saved_accounts if str(acc.pk) == selected), None)
            if account is None:
                raise forms.ValidationError('حساب انتخاب‌شده نامعتبر است.')
            self.selected_bank_account = account
            return cleaned

        self.selected_bank_account = None
        if not cleaned.get('card_number') and not cleaned.get('iban'):
            raise forms.ValidationError('برای حساب جدید، حداقل شماره کارت یا شماره شبا را وارد کنید.')
        if not cleaned.get('account_holder'):
            self.add_error('account_holder', 'نام صاحب حساب الزامی است.')
        return cleaned

    def resolve_bank_account(self):
        """
        حساب نهایی برای create_return_request: یا همان حساب ذخیره‌شده‌ی انتخاب‌شده، یا یک
        UserBankAccount تازه از فیلدهای دستی (که برای دفعات بعد هم ذخیره می‌ماند) - دقیقاً
        هم‌الگوی wallet.forms.WithdrawalRequestForm.resolve_bank_account.
        """
        if self.cleaned_data.get('refund_method') != 'bank':
            return None
        if self.selected_bank_account is not None:
            return self.selected_bank_account

        full_name = self.cleaned_data.get('account_holder') or ''
        parts = full_name.split(None, 1)
        first_name, last_name = (parts[0], parts[1]) if len(parts) == 2 else (full_name, full_name)
        account, _created = UserBankAccount.objects.get_or_create(
            user=self.user, card_number=self.cleaned_data.get('card_number') or '',
            iban=self.cleaned_data.get('iban') or '',
            defaults={'account_holder_first_name': first_name, 'account_holder_last_name': last_name},
        )
        return account
