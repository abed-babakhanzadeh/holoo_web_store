"""
فرم‌های سطح وب باشگاه مشتریان (Loyalty Phase 4D-2). فقط اعتبارسنجی سطحیِ فرمت ورودی؛ هیچ قاعده‌ی
مالی/کسب‌وکاری (کف/سقف/سقف روزانه/موجودی) اینجا بررسی نمی‌شود - آن‌ها منحصراً کارِ Service Layer
هستند (loyalty/redemption.py) و View فقط استثنای آن‌ها را می‌گیرد.
"""

import uuid

from django import forms


class RedeemPointsForm(forms.Form):
    """
    idempotency_token: هم‌الگوی loyalty/admin.py::ManualAdjustmentForm - فقط در رندر اول (GET،
    حالت unbound) تازه تولید می‌شود؛ بعد با خودِ فرم (پنهان) رفت‌وبرگشت می‌کند و View آن را به
    idempotency_key سرویس تبدیل می‌کند - محافظت در برابر دابل‌کلیک/دابل‌ساب‌میت.
    """
    points = forms.IntegerField(
        label='تعداد امتیاز برای تبدیل', min_value=1,
        widget=forms.NumberInput(attrs={
            'class': 'w-full px-4 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-primary '
                     'focus:border-transparent dark:bg-gray-700 dark:border-gray-600 dark:text-white',
        }),
    )
    idempotency_token = forms.CharField(widget=forms.HiddenInput(), required=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.fields['idempotency_token'].initial = uuid.uuid4().hex
