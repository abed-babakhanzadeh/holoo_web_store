import logging
from django.conf import settings

logger = logging.getLogger(__name__)

def send_sms(phone_number: str, message: str) -> bool:
    """
    سرویس جامع ارسال پیامک. 
    در تمام بخش‌های سایت (سفارش، ثبت‌نام، ادمین) از این تابع استفاده می‌شود.
    """
    sms_enabled = getattr(settings, 'REAL_SMS_ENABLED', False)
    
    if not sms_enabled:
        # --- سیستم پیامک مجازی (Mock) ---
        print("\n" + "✉️"*25)
        print(f"📱 [MOCK SMS] To: {phone_number}")
        print(f"💬 Message: {message}")
        print("✉️"*25 + "\n")
        return True
    else:
        # --- سیستم پیامک واقعی (Production) ---
        # در آینده کدهای اتصال به Kavenegar یا FarazSMS اینجا قرار می‌گیرد
        try:
            # api = KavenegarAPI('YOUR_API_KEY')
            # api.sms_send({"receptor": phone_number, "message": message})
            return True
        except Exception as e:
            logger.error(f"Error sending real SMS: {str(e)}")
            return False

def send_otp_sms(phone_number: str, code: str) -> bool:
    """ متد اختصاصی برای ارسال کد یکبار مصرف (ورود/ثبت‌نام) """
    message = f"کد تایید شما برای ورود به فروشگاه: {code}"
    return send_sms(phone_number, message)
