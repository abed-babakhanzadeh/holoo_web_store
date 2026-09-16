"""
هندلر لاگ برای هشدارهای بحرانی.

مسئله‌ای که حل می‌کند: تسک‌های هلو و مسیر پرداخت در نقاط بحرانی logger.critical می‌زنند
(«سفارش X چند روز است در هلو ثبت نشده»، «ناهماهنگی مبلغ پرداخت»، ...). این پیام‌ها تا امروز
فقط در کنسول می‌رفتند و در عمل هیچ‌کس نمی‌دید. با این هندلر، هر لاگ CRITICAL مستقیم به
مدیر اطلاع داده می‌شود.

دو محافظ مهم دارد:
  ۱. ضدحلقه: لاگ‌های خودِ اپ notifications نادیده گرفته می‌شوند، وگرنه شکست ارسال پیام
     باعث لاگ بحرانی و آن باعث تلاش دوباره برای ارسال پیام می‌شد.
  ۲. ضداسپم: پیام تکراری در بازه‌ی ALERT_COOLDOWN فقط یک‌بار فرستاده می‌شود؛ یک خطای
     تکرارشونده نباید صدها پیامک بفرستد.
"""

import logging

ALERT_COOLDOWN = 3600  # ثانیه


class AdminAlertHandler(logging.Handler):
    """ لاگ‌های CRITICAL را به‌صورت اطلاع‌رسانی برای مدیر سایت می‌فرستد """

    def emit(self, record):
        # هیچ خطایی در مسیر لاگ‌گیری نباید باعث شکست عملیات اصلی شود
        try:
            if record.name.startswith('notifications'):
                return  # ضدحلقه

            from django.core.cache import cache
            from notifications.service import notify_admin

            message = self.format(record)
            key = f'admin_alert:{hash(record.name + record.getMessage())}'
            if not cache.add(key, '1', ALERT_COOLDOWN):
                return  # همین هشدار به‌تازگی فرستاده شده

            notify_admin('critical_alert', message=message[:400])
        except Exception:  # noqa: BLE001 - عمداً همه چیز بلعیده می‌شود
            pass
