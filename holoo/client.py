import logging
from django.conf import settings
import requests

logger = logging.getLogger(__name__)

class HolooClient:
    """
    کلاینت ارتباطی با وب‌سرویس نرم‌افزار هلو
    """
    def __init__(self):
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
            "db": settings.HOLOO_DB_NAME
        }
        try:
            response = requests.post(url, data=payload, timeout=10)
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Holoo Login Failed: {e}")
            return {"status": "error", "message": str(e)}

    def insert_person(self, first_name, last_name, phone_number, national_code, address=None):
        """ 
        درج شخص جدید در هلو و دریافت erp_code 
        آرگومان address اختیاریه (default=None)
        """
        if self.is_mock:
            import time
            time.sleep(2) 
            return {
                "success": True, 
                "erp_code": f"ERP_{phone_number[-4:]}",
                "message": "شخص با موفقیت ثبت شد"
            }

        # کدهای واقعی برای ارسال به API هلو
        url = f"{self.base_url}/InsertPerson"
        payload = {
            "FirstName": first_name,
            "LastName": last_name,
            "Mobile": phone_number,
            "NationalCode": national_code,
            "Address": address or ""
        }
        try:
            # فرض می‌کنیم اول باید لاگین کنی
            login_result = self.login()
            if login_result.get("status") != "success":
                return {"success": False, "message": "Login failed"}
            
            headers = {"Authorization": f"Bearer {login_result.get('token')}"}
            response = requests.post(url, json=payload, headers=headers, timeout=10)
            data = response.json()
            
            return {
                "success": True,
                "erp_code": data.get("ErpCode"),
                "message": "شخص با موفقیت ثبت شد"
            }
        except requests.RequestException as e:
            logger.error(f"Holoo InsertPerson Failed: {e}")
            return {"success": False, "message": str(e)}

    def update_person(self, erp_code, first_name=None, last_name=None, address=None, **kwargs):
        """ 
        به‌روزرسانی اطلاعات شخص موجود در هلو 
        """
        if self.is_mock:
            import time
            time.sleep(1)
            return {
                "success": True,
                "message": "اطلاعات شخص با موفقیت به‌روز شد"
            }

        # کدهای واقعی برای API هلو
        url = f"{self.base_url}/UpdatePerson"
        payload = {"ErpCode": erp_code}
        
        if first_name:
            payload["FirstName"] = first_name
        if last_name:
            payload["LastName"] = last_name
        if address:
            payload["Address"] = address
        
        # سایر فیلدها رو هم اضافه کن
        payload.update(kwargs)

        try:
            login_result = self.login()
            if login_result.get("status") != "success":
                return {"success": False, "message": "Login failed"}
            
            headers = {"Authorization": f"Bearer {login_result.get('token')}"}
            response = requests.post(url, json=payload, headers=headers, timeout=10)
            
            return {"success": True, "message": "اطلاعات شخص به‌روز شد"}
        except requests.RequestException as e:
            logger.error(f"Holoo UpdatePerson Failed: {e}")
            return {"success": False, "message": str(e)}

    def get_products(self):
        """ دریافت لیست کالاها از وب‌سرویس هلو """
        if self.is_mock:
            return {
                "product": [
                    {
                        "Code": "00215003",
                        "Name": "لپ تاپ ایسوس مدل ZenBook",
                        "Few": "15.0",
                        "SellPrice": "47500000.0",
                        "SellPrice3": "41000000.0",
                        "UnitName": "دستگاه", 
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
                        "UnitName": "عدد", 
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
        
    def insert_preinvoice(self, payload):
        """ 
        درج پیش فاکتور جدید در هلو
        """
        if self.is_mock:
            import time
            import random
            time.sleep(1.5) # شبیه‌سازی تاخیر شبکه
            mock_code = f"PRE_{random.randint(10000, 99999)}"
            return {
                "success": True,
                "PreInvoiceCode": mock_code,
                "message": "پیش فاکتور با موفقیت در حالت تست (Mock) ثبت شد"
            }

        # کدهای واقعی برای اتصال نهایی
        url = f"{self.base_url}/PreInvoice"
        # بسته به مستندات هلو، اگر با توکن کار می‌کند مثل متدهای بالا بنویسید، 
        # اگر با apikey کار می‌کند مثل خط زیر:
        headers = {'apikey': getattr(settings, 'HOLOO_API_KEY', '')}
        
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=10)
            if response.status_code in [200, 201]:
                data = response.json()
                return {
                    "success": True,
                    "PreInvoiceCode": data.get('PreInvoiceCode', 'نامشخص'),
                    "message": "پیش فاکتور با موفقیت در هلو ثبت شد"
                }
            else:
                return {"success": False, "message": response.text}
        except requests.RequestException as e:
            logger.error(f"Holoo Insert PreInvoice Failed: {e}")
            return {"success": False, "message": str(e)}