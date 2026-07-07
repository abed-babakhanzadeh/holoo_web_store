import logging
from django.conf import settings

logger = logging.getLogger(__name__)

def send_otp_sms(phone_number: str, code: str) -> bool:
    """
    سرویس ارسال پیامک کد یکبار مصرف.
    در حالت توسعه، پیامک را در ترمینال چاپ می‌کند.
    """
    # خواندن وضعیت فعال بودن پنل واقعی از تنظیمات پروژه (به صورت پیش‌فرض False)
    sms_enabled = getattr(settings, 'REAL_SMS_ENABLED', False)
    
    if not sms_enabled:
        # --- سیستم پیامک مجازی (Development / Mock) ---
        print("\n" + "="*50)
        print(f"📱 [MOCK SMS SERVER] Sending SMS to: {phone_number}")
        print(f"💬 Message: کد تایید شما برای ورود به فروشگاه هلو: {code}")
        print("="*50 + "\n")
        return True
    
    else:
        # --- سیستم پیامک واقعی (Production) ---
        # در آینده کدهای متصل به وب‌سرویس پنل پیامک (مثلا Kavenegar یا MeliSMS) اینجا قرار می‌گیرد.
        try:
            # api = KavenegarAPI('YOUR_API_KEY')
            # ...
            return True
        except Exception as e:
            logger.error(f"Error sending real SMS: {str(e)}")
            return False