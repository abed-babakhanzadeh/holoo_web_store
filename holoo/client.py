import base64
import json
import logging
import time
from django.conf import settings
import requests

logger = logging.getLogger(__name__)

# کش درون‌فرآیندیِ ساده برای توکن لاگین هلو (به‌اضافه‌ی زمان انقضا به ثانیه، epoch).
# چون فعلاً فقط تسک زمان‌بندی‌شده‌ی سینک محصول به‌طور مکرر از این کلاینت استفاده می‌کند،
# نیازی به کش مشترک بین پروسه‌ها (Redis) نیست؛ اگر بعداً لازم شد می‌توان ارتقا داد.
_token_cache = {"full_token": None, "exp": 0}


def _decode_jwt_exp(jwt_token):
    """ دیکد بخش payload توکن JWT (بدون بررسی امضا) برای خواندن claim به نام exp """
    try:
        payload_b64 = jwt_token.split('.')[1]
        padded = payload_b64 + '=' * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        return payload.get('exp')
    except Exception:
        return None


class HolooClient:
    """
    کلاینت ارتباطی با وب‌سرویس نرم‌افزار هلو
    """
    def __init__(self):
        self.base_url = getattr(settings, 'HOLOO_API_URL', 'http://127.0.0.1:8080/TncHoloo/api')
        self.is_mock = getattr(settings, 'HOLOO_MOCK_MODE', True)
        # فقط خواندن کالاها (login/get_products/get_product_count) از این پرچم جدا تبعیت می‌کند؛
        # سایر متدها (insert_person/insert_invoice/...) هنوز فقط از is_mock عمومی پیروی می‌کنند
        # چون آن endpointها هنوز طبق مستندات واقعی هلو تأیید/پیاده نشده‌اند.
        self.products_mock = getattr(settings, 'HOLOO_PRODUCTS_MOCK_MODE', self.is_mock)

    def login(self):
        """ لاگین به سرویس هلو طبق مستندات (فقط برای مسیرهای خواندن کالا استفاده می‌شود) """
        if self.products_mock:
            return {"status": "success", "token": "mock_token_123"}

        if _token_cache["full_token"] and _token_cache["exp"] > time.time():
            return {"status": "success", "token": _token_cache["full_token"].removeprefix("Bearer ").strip()}

        url = f"{self.base_url}/Login"
        payload = {
            "userinfo": {
                "username": settings.HOLOO_USERNAME,
                # HOLOO_PASSWORD از قبل به‌صورت base64 نگه داشته می‌شه (دقیقاً همون مقداری که در Swagger
                # وارد می‌کنید)، پس این‌جا مستقیم و بدون انکود مجدد فرستاده می‌شه.
                "userpass": settings.HOLOO_PASSWORD,
                "dbname": settings.HOLOO_DB_NAME,
            }
        }
        headers = {"Authorization": getattr(settings, 'HOLOO_LOGIN_AUTH_HEADER', '123')}
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=15)
            response.raise_for_status()
            login_data = response.json().get('Login', {})
            if not login_data.get('State'):
                return {"status": "error", "message": login_data.get('Error') or "Holoo login rejected (State=false)"}

            full_token = login_data.get('Token')
            if not full_token:
                return {"status": "error", "message": "No token in Holoo login response"}

            exp = _decode_jwt_exp(full_token.removeprefix("Bearer ").strip())
            ttl = max(exp - int(time.time()) - 60, 30) if exp else 15 * 60  # حاشیه‌ی ۶۰ ثانیه، fallback ۱۵ دقیقه
            _token_cache["full_token"] = full_token
            _token_cache["exp"] = time.time() + ttl

            return {"status": "success", "token": full_token.removeprefix("Bearer ").strip()}
        except (requests.RequestException, ValueError) as e:
            logger.error(f"Holoo Login Failed: {e}")
            return {"status": "error", "message": str(e)}

    def _get_auth_header(self):
        """ توکن کامل (با پیشوند Bearer) آماده‌ی استفاده در هدر Authorization؛ در صورت نیاز لاگین می‌کند """
        if self.products_mock:
            return "Bearer mock_token_123"
        if _token_cache["full_token"] and _token_cache["exp"] > time.time():
            return _token_cache["full_token"]
        result = self.login()
        if result.get("status") != "success":
            raise RuntimeError(f"Holoo login failed: {result.get('message')}")
        return _token_cache["full_token"]

    def _authenticated_get(self, url, timeout=30):
        """ GET با هدر Authorization؛ روی ۴۰۱ یک‌بار توکن را باطل کرده و دوباره تلاش می‌کند """
        try:
            response = requests.get(url, headers={"Authorization": self._get_auth_header()}, timeout=timeout)
            if response.status_code == 401 and not self.products_mock:
                _token_cache["full_token"] = None
                _token_cache["exp"] = 0
                response = requests.get(url, headers={"Authorization": self._get_auth_header()}, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError, RuntimeError) as e:
            logger.error(f"Holoo GET {url} failed: {e}")
            return None

    def get_products(self, page=1, items_per_page=500):
        """ دریافت یک صفحه از لیست کالاها از وب‌سرویس هلو """
        if self.products_mock:
            if page != 1:
                return {"product": []}
            return {
                "product": [
                    {
                        "Code": "00215003",
                        "Name": "لپ تاپ ایسوس مدل ZenBook",
                        "Few": 15,
                        "SellPrice": 47500000,
                        "SellPrice2": 0, "SellPrice3": 41000000, "SellPrice4": 0, "SellPrice5": 0,
                        "SellPrice6": 0, "SellPrice7": 0, "SellPrice8": 0, "SellPrice9": 0, "SellPrice10": 0,
                        "MainGroupName": "کالای دیجیتال",
                        "MainGroupErpCode": "bBAHfg==",
                        "SideGroupName": "لپ تاپ",
                        "SideGroupErpCode": "bBAHNA1jDg0=",
                        "IsActive": True,
                        "ErpCode": "bBAHNA1mckd4QB4O"
                    },
                    {
                        "Code": "00215004",
                        "Name": "گوشی سامسونگ Galaxy S23",
                        "Few": 8,
                        "SellPrice": 52000000,
                        "SellPrice2": 0, "SellPrice3": 0, "SellPrice4": 0, "SellPrice5": 0,
                        "SellPrice6": 0, "SellPrice7": 0, "SellPrice8": 0, "SellPrice9": 0, "SellPrice10": 0,
                        "MainGroupName": "کالای دیجیتال",
                        "MainGroupErpCode": "bBAHfg==",
                        "SideGroupName": "موبایل",
                        "SideGroupErpCode": "bBAHNA1jDg1=",
                        "IsActive": True,
                        "ErpCode": "bBAHNA1mckd4QB4P"
                    }
                ]
            }
        return self._authenticated_get(f"{self.base_url}/Product/{page}/{items_per_page}")

    def get_product_count(self):
        """ تعداد کل کالاهای موجود در هلو (برای چک کامل بودن واکشی صفحه‌بندی‌شده) """
        if self.products_mock:
            return 2
        data = self._authenticated_get(f"{self.base_url}/Product/count")
        if data is None:
            return None
        if isinstance(data, int):
            return data
        if isinstance(data, dict):
            for key in ('totalCount', 'count', 'Count'):
                if key in data:
                    try:
                        return int(data[key])
                    except (TypeError, ValueError):
                        return None
        logger.warning(f"Unrecognized shape for Holoo /Product/count response: {data!r}")
        return None

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

    def insert_invoice(self, payload):
        """
        درج فاکتور در هلو.
        طبق تصمیم کارفرما، پیش‌فاکتور اصلاً در هلو ثبت نمی‌شود؛ صرف‌نظر از روش پرداخت
        (نقدی/چکی/اقساطی) همیشه فاکتور قطعی ثبت می‌شود و نحوه تسویه (چک/نقد/...) بعداً
        در بخش مالی هلو مشخص می‌شود.
        """
        if self.is_mock:
            import time
            import random
            time.sleep(1.5) # شبیه‌سازی تاخیر شبکه
            mock_code = f"INV_{random.randint(10000, 99999)}"
            return {
                "success": True,
                "InvoiceCode": mock_code,
                "message": "فاکتور با موفقیت در حالت تست (Mock) ثبت شد"
            }

        # کدهای واقعی برای اتصال نهایی
        url = f"{self.base_url}/Invoice"
        # بسته به مستندات هلو، اگر با توکن کار می‌کند مثل متدهای بالا بنویسید،
        # اگر با apikey کار می‌کند مثل خط زیر:
        headers = {'apikey': getattr(settings, 'HOLOO_API_KEY', '')}

        try:
            response = requests.post(url, json=payload, headers=headers, timeout=10)
            if response.status_code in [200, 201]:
                data = response.json()
                return {
                    "success": True,
                    "InvoiceCode": data.get('InvoiceCode', 'نامشخص'),
                    "message": "فاکتور با موفقیت در هلو ثبت شد"
                }
            else:
                return {"success": False, "message": response.text}
        except requests.RequestException as e:
            logger.error(f"Holoo Insert Invoice Failed: {e}")
            return {"success": False, "message": str(e)}

    def register_payment(self, invoice_code, amount):
        """
        ثبت سند دریافت وجه برای فاکتوری که قبلاً در هلو ثبت شده، پس از پرداخت آنلاین موفق.
        (نوع تسویه - نقد/چک/... - طبق تصمیم کارفرما فعلاً در همین مرحله وارد جزئیات هلو نمی‌شود
        و بعداً در بخش مالی هلو مشخص خواهد شد.)
        """
        if self.is_mock:
            import time
            time.sleep(2.5) # شبیه‌سازی تاخیر شبکه/زمان پردازش درخواست در هلو
            return {
                "success": True,
                "ReceiptCode": f"RCP_{invoice_code.split('_')[-1]}",
                "message": "سند دریافت وجه با موفقیت در حالت تست (Mock) ثبت شد"
            }

        # کدهای واقعی برای اتصال نهایی
        url = f"{self.base_url}/PaymentReceipt"
        payload = {"InvoiceCode": invoice_code, "Amount": amount}
        headers = {'apikey': getattr(settings, 'HOLOO_API_KEY', '')}

        try:
            response = requests.post(url, json=payload, headers=headers, timeout=10)
            if response.status_code in [200, 201]:
                data = response.json()
                return {
                    "success": True,
                    "ReceiptCode": data.get('ReceiptCode', 'نامشخص'),
                    "message": "سند دریافت وجه با موفقیت در هلو ثبت شد"
                }
            else:
                return {"success": False, "message": response.text}
        except requests.RequestException as e:
            logger.error(f"Holoo Register Payment Failed: {e}")
            return {"success": False, "message": str(e)}
