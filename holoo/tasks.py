import logging
from celery import shared_task
from django.apps import apps
from .client import HolooClient

logger = logging.getLogger(__name__)

@shared_task(bind=True, max_retries=3)
def sync_user_to_holoo(self, user_id):
    """
    تسک پس‌زمینه برای ارسال اطلاعات کاربر به هلو.
    """
    # 1. ایمپورت مستقیم کلاس وضعیت (چون مدل دیتابیسی نیست)
    from accounts.models import UserStatus 
    
    # 2. فراخوانی مدل کاربر با get_model (چون مدل دیتابیسی است)
    CustomUser = apps.get_model('accounts', 'CustomUser')
    
    try:
        user = CustomUser.objects.get(id=user_id)
    except CustomUser.DoesNotExist:
        return "User not found."

    logger.info(f"شروع همگام‌سازی کاربر {user.phone_number} با هلو...")
    
    # استفاده از کلاینت هلو
    client = HolooClient()
    result = client.insert_person(
        first_name=user.first_name,
        last_name=user.last_name,
        phone_number=user.phone_number,
        national_code=user.national_code
    )

    if result.get('success'):
        # همگام‌سازی موفقیت‌آمیز بود
        user.erp_code = result.get('erp_code')
        user.status = UserStatus.ACTIVE
        user.last_sync_error = None
        user.save()
        logger.info(f"کاربر {user.phone_number} با موفقیت در هلو ثبت شد. کد: {user.erp_code}")
        return "Sync Success"
    else:
        error_msg = result.get('message', 'خطای نامشخص از سمت هلو')
        user.last_sync_error = error_msg
        user.retry_count += 1
        user.save()
        
        logger.warning(f"خطا در ثبت کاربر {user.phone_number} در هلو: {error_msg}")
        raise self.retry(countdown=60)