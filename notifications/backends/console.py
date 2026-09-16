"""موتور توسعه: پیام را در ترمینال چاپ می‌کند و چیزی نمی‌فرستد."""

import logging
import uuid

from .base import NotificationBackend

logger = logging.getLogger(__name__)


class ConsoleBackend(NotificationBackend):
    def send(self, to: str, text: str) -> str:
        # چاپ با try/except: روی کنسول ویندوز با کدپیج غیر UTF-8، ایموجی/فارسی باعث
        # UnicodeEncodeError می‌شود. نبود امکان «چاپ» نباید ارسال را شکست‌خورده حساب کند.
        try:
            print("\n" + "=" * 52)
            print(f"[NOTIFICATION] to: {to}")
            print(text)
            print("=" * 52 + "\n")
        except UnicodeEncodeError:
            logger.info("[NOTIFICATION] to %s (متن به‌دلیل انکودینگ کنسول چاپ نشد)", to)
        return f"console-{uuid.uuid4().hex[:12]}"
