"""
موتور پیامک کاوه‌نگار.

اسکلت آماده است؛ فقط KAVENEGAR_API_KEY را در تنظیمات بگذارید و
NOTIFICATION_BACKEND = 'notifications.backends.kavenegar.KavenegarBackend' کنید.
"""

import logging

import requests
from django.conf import settings

from .base import NotificationBackend, NotificationBackendError

logger = logging.getLogger(__name__)

API_URL = 'https://api.kavenegar.com/v1/{api_key}/sms/send.json'
TIMEOUT = 10


class KavenegarBackend(NotificationBackend):
    def send(self, to: str, text: str) -> str:
        api_key = getattr(settings, 'KAVENEGAR_API_KEY', '')
        if not api_key:
            # پیکربندی ناقص یک خطای دائمی است، نه موقت؛ ولی چون اپراتور می‌تواند کلید را
            # اضافه کند و تلاش مجدد موفق شود، همان خطای قابل‌تلاش‌مجدد را می‌اندازیم تا
            # پیام گم نشود.
            raise NotificationBackendError("KAVENEGAR_API_KEY تنظیم نشده است.")

        payload = {'receptor': to, 'message': text}
        sender = getattr(settings, 'KAVENEGAR_SENDER', '')
        if sender:
            payload['sender'] = sender

        try:
            response = requests.post(API_URL.format(api_key=api_key), data=payload, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise NotificationBackendError(f"خطای شبکه در ارتباط با کاوه‌نگار: {e}") from e

        if response.status_code != 200:
            raise NotificationBackendError(f"کاوه‌نگار کد {response.status_code} برگرداند: {response.text[:200]}")

        try:
            data = response.json()
        except ValueError as e:
            raise NotificationBackendError(f"پاسخ کاوه‌نگار JSON معتبر نبود: {response.text[:200]}") from e

        status = (data.get('return') or {}).get('status')
        if status != 200:
            raise NotificationBackendError(f"کاوه‌نگار پیام را رد کرد: {data.get('return')}")

        entries = data.get('entries') or []
        return str(entries[0].get('messageid')) if entries else ''
