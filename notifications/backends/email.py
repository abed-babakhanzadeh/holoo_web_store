"""
موتور ایمیل — نمونه‌ی عملی اینکه تعویض کانال هیچ تغییری در بقیه‌ی پروژه لازم ندارد.

`to` اینجا یک آدرس ایمیل است به‌جای شماره موبایل؛ چون قرارداد فقط «مقصد + متن» است،
هیچ‌کدام از فراخوان‌کننده‌ها (سفارش، پرداخت، ثبت‌نام، هشدار هلو) نیازی به تغییر ندارند.
"""

import uuid

from django.conf import settings
from django.core.mail import send_mail

from .base import NotificationBackend, NotificationBackendError


class EmailBackend(NotificationBackend):
    def send(self, to: str, text: str) -> str:
        subject = getattr(settings, 'NOTIFICATION_EMAIL_SUBJECT', 'فروشگاه هلو')
        # خط اول متن، عنوان ایمیل می‌شود (اگر کوتاه باشد) تا ایمیل عنوان معناداری داشته باشد
        first_line = text.strip().splitlines()[0] if text.strip() else ''
        if 0 < len(first_line) <= 80:
            subject = first_line

        try:
            sent = send_mail(
                subject=subject,
                message=text,
                from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None),
                recipient_list=[to],
                fail_silently=False,
            )
        except Exception as e:
            raise NotificationBackendError(f"ارسال ایمیل ناموفق بود: {e}") from e

        if not sent:
            raise NotificationBackendError("ارسال ایمیل ناموفق بود (هیچ پیامی تحویل داده نشد).")
        return f"email-{uuid.uuid4().hex[:12]}"
