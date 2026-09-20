"""
فرم تسویه حساب.

مشخصات گیرنده، آدرس و کرایه دیگر از فرم نمی‌آیند: فقط «شناسه‌ی آدرس» و روش پرداخت. آدرس با فیلتر مالک از دیتابیس
خوانده می‌شود و گیرنده/مقصد/ارسال از روی آن اسنپ‌شات می‌شود (orders/checkout.py و orders/snapshot.py).
"""

from django import forms

from products.pricing import VALID_PAYMENT_METHODS


class CheckoutForm(forms.Form):
    address_id = forms.CharField(required=False)
    payment_method = forms.CharField(required=False)

    def clean_address_id(self):
        # مقدار خام (رشته) برمی‌گردد؛ تبدیل و مالک‌سنجی در get_user_address انجام می‌شود
        return (self.cleaned_data.get('address_id') or '').strip()

    def clean_payment_method(self):
        # اعتبارسنجی نهایی (قفل کاربر ویژه و مقدار نامعتبر) در products.pricing انجام می‌شود؛
        # اینجا فقط مقادیر آشکارا بی‌ربط را دور می‌ریزیم
        value = (self.cleaned_data.get('payment_method') or '').strip()
        return value if value in VALID_PAYMENT_METHODS else ''
