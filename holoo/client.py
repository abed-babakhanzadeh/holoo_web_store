import logging
from django.conf import settings
import requests # باید نصب شود: pip install requests

logger = logging.getLogger(__name__)

class HolooClient:
    """
    کلاینت ارتباطی با وب‌سرویس نرم‌افزار هلو
    """
    def __init__(self):
        # این متغیرها را بعداً در settings.py اضافه می‌کنیم
        self.base_url = getattr(settings, 'HOLOO_API_URL', 'http://127.0.0.1:8080/TncHoloo/api')
        self.is_mock = getattr(settings, 'HOLOO_MOCK_MODE', True) 
        
    def login(self):
        """ لاگین به سرویس هلو طبق مستندات """
        if self.is_mock:
            return {"status": "success", "token": "mock_token_123"}
            
        url = f"{self.base_url}/Login"
        payload = {
            "username": settings.HOLOO_USERNAME,
            "userpass": settings.HOLOO_PASSWORD,
            "Db": settings.HOLOO_DB_NAME
        }
        try:
            response = requests.post(url, data=payload, timeout=10)
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Holoo Login Failed: {e}")
            return {"status": "error", "message": str(e)}

    def insert_person(self, first_name, last_name, phone_number, national_code):
        """ درج شخص جدید در هلو و دریافت erp_code """
        if self.is_mock:
            # شبیه‌سازی یک اتصال موفق پس از 2 ثانیه
            import time
            time.sleep(2) 
            return {
                "success": True, 
                "erp_code": f"ERP_{phone_number[-4:]}", # تولید یک کد مجازی
                "message": "شخص با موفقیت ثبت شد"
            }

        # کدهای واقعی برای ارسال به API هلو (بعداً بر اساس ساختار دقیق جیسون هلو تکمیل می‌شود)
        # url = f"{self.base_url}/InsertPerson"
        # ...
        
    def get_products(self):
        """ دریافت لیست کالاها از وب‌سرویس هلو """
        if self.is_mock:
            # شبیه‌سازی دقیق خروجی جیسون هلو بر اساس مستندات API
            return {
                "product": [
                    {
                        "Code": "00215003",
                        "Name": "لپ تاپ ایسوس مدل ZenBook",
                        "Few": "15.0",
                        "SellPrice": "45000000.0",
                        "MainGroupName": "کالای دیجیتال",
                        "MainGroupErpCode": "bBAHfg==",
                        "SideGroupName": "لپ تاپ",
                        "SideGroupErpCode": "bBAHNA1jDg0=",
                        "ErpCode": "bBAHNA1mckd4QB4O"
                    },
                    {
                        "Code": "00215004",
                        "Name": "گوشی سامسونگ Galaxy S23",
                        "Few": "8.0",
                        "SellPrice": "52000000.0",
                        "MainGroupName": "کالای دیجیتال",
                        "MainGroupErpCode": "bBAHfg==",
                        "SideGroupName": "موبایل",
                        "SideGroupErpCode": "bBAHNA1jDg1=",
                        "ErpCode": "bBAHNA1mckd4QB4P"
                    }
                ]
            }

        # کدهای واقعی برای آینده
        # url = f"{self.base_url}/Product"
        # response = requests.get(url, headers={"Authorization": ...})
        # return response.json()