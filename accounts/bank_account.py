"""
مدل حساب بانکی کاربر (چندحسابی) - هم‌الگوی دقیق accounts/address.py برای مدیریت «پیش‌فرض»:
قفل ردیف کاربر (select_for_update) + تضمین اینکه کاربر بدون حساب، پیش‌فرض ندارد و کاربر با
حداقل یک حساب، دقیقاً یک پیش‌فرض دارد. تمام منطق در save()/delete() است تا ادمین، فرم برداشت
کیف‌پول، فرم مرجوعی و شل همه از یک مسیر رد شوند.
"""

import re

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .address import _lock_user_rows

_IBAN_DIGITS_RE = re.compile(r'^\d{24}$')
_CARD_DIGITS_RE = re.compile(r'^\d{16}$')


class UserBankAccountQuerySet(models.QuerySet):
    def delete(self):
        """
        حذف گروهی (مثلاً action ادمین): کاربرهای درگیر پیش از حذف قفل می‌شوند (همان ترتیبِ
        save/delete: اول کاربر، بعد ردیف‌ها) تا با save هم‌زمان روی همان کاربر بن‌بست نشود -
        هم‌الگوی دقیق AddressQuerySet.delete در accounts/address.py.
        """
        user_ids = sorted(set(self.values_list('user_id', flat=True)))
        with transaction.atomic():
            _lock_user_rows(user_ids)
            return super().delete()


class UserBankAccount(models.Model):
    user = models.ForeignKey('accounts.CustomUser', on_delete=models.CASCADE, related_name='bank_accounts', verbose_name='کاربر')

    account_holder_first_name = models.CharField(max_length=50, verbose_name='نام صاحب حساب')
    account_holder_last_name = models.CharField(max_length=50, verbose_name='نام خانوادگی صاحب حساب')
    # حداقل یکی از این دو الزامی است (نگاه کنید _validation_errors) - کاربر می‌تواند فقط کارت،
    # فقط شبا، یا هر دو را ثبت کند
    card_number = models.CharField(max_length=16, blank=True, verbose_name='شماره کارت (۱۶ رقم)')
    iban = models.CharField(max_length=26, blank=True, verbose_name='شماره شبا')

    is_default = models.BooleanField(default=False, verbose_name='حساب پیش‌فرض')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='تاریخ ثبت')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='آخرین ویرایش')

    objects = UserBankAccountQuerySet.as_manager()

    class Meta:
        verbose_name = 'حساب بانکی'
        verbose_name_plural = 'حساب‌های بانکی'
        ordering = ('-is_default', '-created_at', '-id')
        constraints = [
            models.UniqueConstraint(fields=('user',), condition=Q(is_default=True), name='unique_default_bank_account_per_user'),
            models.CheckConstraint(condition=Q(card_number__gt='') | Q(iban__gt=''), name='bank_account_requires_card_or_iban'),
        ]

    def __str__(self):
        holder = f'{self.account_holder_first_name} {self.account_holder_last_name}'.strip()
        tail = self.card_number[-4:] if self.card_number else (self.iban[-4:] if self.iban else '')
        return f'{holder} - ****{tail}' if tail else holder

    @property
    def account_holder_full_name(self):
        return f'{self.account_holder_first_name} {self.account_holder_last_name}'.strip()

    @property
    def masked_display(self):
        """ نمایش کوتاه برای کمبوباکس فرم‌ها (برداشت کیف‌پول / مرجوعی) """
        if self.card_number:
            return f'{self.card_number[:4]}-****-****-{self.card_number[-4:]} ({self.account_holder_full_name})'
        if self.iban:
            return f'{self.iban[:6]}...{self.iban[-4:]} ({self.account_holder_full_name})'
        return self.account_holder_full_name

    # ------------------------------------------------------------------ اعتبارسنجی
    def _validation_errors(self):
        errors = {}
        if not (self.account_holder_first_name or '').strip():
            errors['account_holder_first_name'] = 'نام صاحب حساب الزامی است.'
        if not (self.account_holder_last_name or '').strip():
            errors['account_holder_last_name'] = 'نام خانوادگی صاحب حساب الزامی است.'

        card = (self.card_number or '').strip()
        iban = (self.iban or '').strip().upper().replace(' ', '')
        if card and not _CARD_DIGITS_RE.match(card):
            errors['card_number'] = 'شماره کارت باید دقیقاً ۱۶ رقم باشد.'
        if iban:
            digits = iban[2:] if iban.startswith('IR') else iban
            if not _IBAN_DIGITS_RE.match(digits):
                errors['iban'] = 'شماره شبا باید ۲۴ رقم باشد (با یا بدون پیشوند IR).'
            else:
                self.iban = f'IR{digits}'
        if not card and not iban:
            errors.setdefault('card_number', 'حداقل یکی از شماره کارت یا شبا الزامی است.')
        return errors

    def clean(self):
        errors = self._validation_errors()
        if errors:
            raise ValidationError(errors)

    # ------------------------------------------------------------------ ذخیره / حذف
    def save(self, *args, **kwargs):
        update_fields = kwargs.get('update_fields')
        if update_fields is None or not set(update_fields) <= {'is_default', 'updated_at'}:
            errors = self._validation_errors()
            if errors:
                raise ValidationError(errors)

        with transaction.atomic():
            _lock_user_rows([self.user_id])
            siblings = UserBankAccount.objects.filter(user_id=self.user_id)
            if self.pk:
                siblings = siblings.exclude(pk=self.pk)

            wanted_default = self.is_default
            if not siblings.exists():
                self.is_default = True                        # اولین حساب کاربر همیشه پیش‌فرض است
            elif self.is_default:
                siblings.filter(is_default=True).update(is_default=False)
            elif not siblings.filter(is_default=True).exists():
                self.is_default = True                        # پیش‌فرضِ کاربر نباید بدون جانشین خاموش شود

            if update_fields is not None and self.is_default != wanted_default:
                kwargs['update_fields'] = set(update_fields) | {'is_default'}

            super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            _lock_user_rows([self.user_id])
            current = list(UserBankAccount.objects.filter(pk=self.pk).values_list('is_default', flat=True))
            if current:
                self.is_default = current[0]
            return super().delete(*args, **kwargs)

    def set_default(self):
        self.is_default = True
        self.save(update_fields=['is_default', 'updated_at'])


@receiver(post_delete, sender=UserBankAccount, dispatch_uid='accounts_bank_account_promote_default')
def promote_default_bank_account_after_delete(sender, instance, **kwargs):
    """ اگر حساب پیش‌فرضِ کاربری پاک شد و حساب دیگری دارد، جدیدترین حساب پیش‌فرض می‌شود """
    if not instance.is_default:
        return
    with transaction.atomic():
        if not _lock_user_rows([instance.user_id]):
            return   # خودِ کاربر پاک شده (cascade)؛ چیزی برای جانشینی نیست
        remaining = UserBankAccount.objects.filter(user_id=instance.user_id)
        if remaining.filter(is_default=True).exists():
            return
        successor = remaining.order_by('-created_at', '-id').first()
        if successor is None:
            return
        UserBankAccount.objects.filter(pk=successor.pk).update(is_default=True)
