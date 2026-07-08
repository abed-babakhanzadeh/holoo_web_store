import logging
from celery import shared_task
from django.apps import apps
from .client import HolooClient

logger = logging.getLogger(__name__)

# max_retries=10 یعنی تا 10 بار تلاش میکنه (طی چند روز!)
@shared_task(bind=True, max_retries=10)
def sync_user_to_holoo(self, user_id):
    from accounts.models import UserStatus 
    CustomUser = apps.get_model('accounts', 'CustomUser')
    
    try:
        user = CustomUser.objects.get(id=user_id)
    except CustomUser.DoesNotExist:
        return "User not found."

    client = HolooClient()
    
    # ---------------------------------------------------------
    # پوکایوکه ۱: تفکیک ساخت مشتری جدید از آپدیت مشتری قدیمی
    # ---------------------------------------------------------
    if user.erp_code:
        # کاربر قبلا در هلو بوده، پس فقط باید آپدیت شود (این متد باید در client ساخته شود)
        logger.info(f"شروع آپدیت کاربر {user.phone_number} در هلو...")
        result = client.update_person(
            erp_code=user.erp_code,
            first_name=user.first_name,
            last_name=user.last_name,
            address=user.address,
            # سایر فیلدها...
        )
    else:
        # مشتری جدید است، باید ساخته شود
        logger.info(f"شروع ثبت مشتری جدید {user.phone_number} در هلو...")
        result = client.insert_person(
            first_name=user.first_name,
            last_name=user.last_name,
            phone_number=user.phone_number,
            national_code=user.national_code,
            address=user.address
        )

    # بررسی نتیجه
    if result.get('success'):
        if not user.erp_code:
            user.erp_code = result.get('erp_code')
        user.status = UserStatus.ACTIVE
        user.last_sync_error = None
        user.retry_count = 0
        user.save()
        return "Sync Success"
    else:
        error_msg = result.get('message', 'خطای نامشخص هلو')
        error_code = result.get('code') # فرض میکنیم کلاینت کد خطا را هم برمیگرداند
        
        user.last_sync_error = error_msg
        user.retry_count += 1
        user.save()
        
        # ---------------------------------------------------------
        # پوکایوکه ۲: توقف تلاش برای خطاهای دیتایی (مثل خطای ۲۳ هلو)
        # ---------------------------------------------------------
        if error_code in ['23', '10', '8']: # کدهای خطای تکراری بودن هلو
            logger.error(f"خطای دیتایی غیرقابل حل: {error_msg}. توقف تلاش.")
            # اینجا وضعیت کاربر را روی PENDING نگه میداریم تا خودش بیاید دیتا را اصلاح کند
            return "Fatal Data Error - No Retry"

        # ---------------------------------------------------------
        # پوکایوکه ۳: تلاش مجدد تصاعدی برای خطاهای شبکه (Exponential Backoff)
        # ---------------------------------------------------------
        # فرمول: (تعداد دفعات تلاش ^ 2) * 60 ثانیه
        # دفعه اول: 1 دقیقه، دفعه دوم: 4 دقیقه، دفعه سوم: 9 دقیقه، دفعه پنجم: 25 دقیقه و ...
        backoff_time = (self.request.retries ** 2) * 60 
        logger.warning(f"خطای ارتباطی: {error_msg}. تلاش مجدد در {backoff_time} ثانیه دیگر...")
        
        raise self.retry(countdown=backoff_time)