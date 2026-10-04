import base64
import json
import logging
import time

import requests

from .conf import get_config

logger = logging.getLogger(__name__)

# کش درون‌فرآیندیِ ساده برای توکن لاگین هلو. کلید کش (آدرس + کاربر + دیتابیس) است تا با عوض شدن دیتابیس
# (مثلاً جابه‌جایی بین دو دیتابیس هلو) توکن دیتابیس قبلی هرگز دوباره استفاده نشود.
_token_cache = {"key": None, "full_token": None, "exp": 0}


def _decode_jwt_exp(jwt_token):
    """ دیکد بخش payload توکن JWT (بدون بررسی امضا) برای خواندن claim به نام exp """
    try:
        payload_b64 = jwt_token.split('.')[1]
        padded = payload_b64 + '=' * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        return payload.get('exp')
    except Exception:
        return None


def _clear_token_cache():
    _token_cache.update({"key": None, "full_token": None, "exp": 0})


class HolooClient:
    """
    کلاینت ارتباطی با وب‌سرویس نرم‌افزار هلو.

    خواندن (login / get_products / get_product_count / get_json) با HOLOO_READ_MODE کنترل می‌شود و واقعی است.
    نوشتن (insert_person / update_person / insert_invoice / register_payment) با HOLOO_WRITE_MODE جدا کنترل می‌شود
    و در این فاز *هرگز* درخواستی به هلو نمی‌فرستد: mock = شبیه‌سازی محلی، disabled = رد صریح بدون هیچ تماس.
    جزئیات و دلیل در holoo/conf.py.
    """
    def __init__(self, config=None):
        self.config = config or get_config()
        self.base_url = self.config.base_url
        self.products_mock = self.config.read_is_mock     # نام قدیمی؛ فقط به پرچم «خواندن» ربط دارد
        self.last_error = None

    # ------------------------------------------------------------------ احراز هویت و خواندن

    def _cache_key(self):
        c = self.config
        return (c.base_url, c.username, c.db_name)

    def _cached_token(self):
        if _token_cache["key"] == self._cache_key() and _token_cache["full_token"] and _token_cache["exp"] > time.time():
            return _token_cache["full_token"]
        return None

    def login(self):
        """ لاگین به سرویس هلو طبق مستندات؛ نتیجه {"status": "success"/"error", ...} (توکن فقط داخل حافظه) """
        if self.products_mock:
            return {"status": "success", "token": "mock_token_123"}

        problems = self.config.problems()
        if problems:
            self.last_error = ' '.join(problems)
            return {"status": "error", "message": self.last_error}

        cached = self._cached_token()
        if cached:
            return {"status": "success", "token": cached.removeprefix("Bearer ").strip()}

        url = f"{self.base_url}/Login"
        payload = {
            "userinfo": {
                "username": self.config.username,
                # رمز از قبل base64 است (دقیقاً همان مقداری که در Swagger وارد می‌شود)؛ مستقیم و بدون انکود مجدد
                "userpass": self.config.password,
                "dbname": self.config.db_name,
            }
        }
        headers = {"Authorization": self.config.login_auth_header}
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=self.config.timeout)
            response.raise_for_status()
            login_data = response.json().get('Login', {})
            if not login_data.get('State'):
                self.last_error = login_data.get('Error') or "Holoo login rejected (State=false)"
                return {"status": "error", "message": self.last_error, "code": login_data.get('ErrorCode')}

            full_token = login_data.get('Token')
            if not full_token:
                self.last_error = "No token in Holoo login response"
                return {"status": "error", "message": self.last_error}

            exp = _decode_jwt_exp(full_token.removeprefix("Bearer ").strip())
            ttl = max(exp - int(time.time()) - 60, 30) if exp else 15 * 60  # حاشیه‌ی ۶۰ ثانیه، fallback ۱۵ دقیقه
            _token_cache.update({"key": self._cache_key(), "full_token": full_token, "exp": time.time() + ttl})

            return {"status": "success", "token": full_token.removeprefix("Bearer ").strip()}
        except (requests.RequestException, ValueError) as e:
            self.last_error = str(e)
            logger.error(f"Holoo Login Failed: {e}")
            return {"status": "error", "message": self.last_error}

    def _get_auth_header(self):
        """ توکن کامل (با پیشوند Bearer) آماده‌ی استفاده در هدر Authorization؛ در صورت نیاز لاگین می‌کند """
        if self.products_mock:
            return "Bearer mock_token_123"
        cached = self._cached_token()
        if cached:
            return cached
        result = self.login()
        if result.get("status") != "success":
            raise RuntimeError(f"Holoo login failed: {result.get('message')}")
        return _token_cache["full_token"]

    def _authenticated_get(self, url, timeout=None, params=None):
        """ GET با هدر Authorization؛ روی ۴۰۱ یک‌بار توکن را باطل کرده و دوباره تلاش می‌کند. تنها راه خواندن از هلو. """
        timeout = timeout or self.config.timeout
        try:
            response = requests.get(url, headers={"Authorization": self._get_auth_header()}, params=params, timeout=timeout)
            if response.status_code == 401 and not self.products_mock:
                _clear_token_cache()
                response = requests.get(url, headers={"Authorization": self._get_auth_header()}, params=params, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError, RuntimeError) as e:
            self.last_error = str(e)
            logger.error(f"Holoo GET {url} failed: {e}")
            return None

    def get_json(self, path, params=None):
        """ GET عمومی و فقط‌خواندنی برای ابزارهای تشخیصی (مثل /Version، /Settings). در حالت mock همیشه None. """
        if self.products_mock:
            return None
        return self._authenticated_get(f"{self.base_url}/{path.lstrip('/')}", params=params)

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
                        "service": False,
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
                        "service": False,
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

    # ------------------------------------------------------------------ نوشتن (در این فاز هرگز واقعی نیست)

    def _write_disabled(self, operation):
        logger.warning("Holoo %s رد شد: HOLOO_WRITE_MODE=disabled (هیچ درخواستی به هلو فرستاده نشد).", operation)
        return {
            "success": False, "disabled": True, "code": "WRITE_DISABLED",
            "message": "نوشتن در هلو غیرفعال است (HOLOO_WRITE_MODE=disabled).",
        }

    def insert_person(self, first_name, last_name, phone_number, national_code, address=None):
        """
        درج شخص جدید در هلو (فقط شبیه‌سازی). آرگومان address اختیاریه (default=None).
        قرارداد واقعی (POST /Customer با custinfo) در فاز «ثبت مشتری» پیاده می‌شود.
        """
        if not self.config.write_is_mock:
            return self._write_disabled('insert_person')
        time.sleep(2)
        return {
            "success": True,
            "erp_code": f"ERP_{phone_number[-4:]}",
            "message": "شخص با موفقیت ثبت شد"
        }

    def update_person(self, erp_code, first_name=None, last_name=None, address=None, **kwargs):
        """ به‌روزرسانی اطلاعات شخص در هلو (فقط شبیه‌سازی؛ قرارداد واقعی PUT /Customer در فاز مشتری) """
        if not self.config.write_is_mock:
            return self._write_disabled('update_person')
        time.sleep(1)
        return {
            "success": True,
            "message": "اطلاعات شخص با موفقیت به‌روز شد"
        }

    def insert_invoice(self, payload):
        """
        درج فاکتور در هلو (فقط شبیه‌سازی؛ قرارداد واقعی POST /Invoice/Invoice در فاز فاکتور).
        طبق تصمیم کارفرما، پیش‌فاکتور اصلاً در هلو ثبت نمی‌شود؛ صرف‌نظر از روش پرداخت
        (نقدی/چکی/اقساطی) همیشه فاکتور قطعی ثبت می‌شود.
        """
        if not self.config.write_is_mock:
            return self._write_disabled('insert_invoice')
        import random
        time.sleep(1.5)  # شبیه‌سازی تاخیر شبکه
        mock_code = f"INV_{random.randint(10000, 99999)}"
        return {
            "success": True,
            "InvoiceCode": mock_code,
            "message": "فاکتور با موفقیت در حالت تست (Mock) ثبت شد"
        }

    def register_payment(self, invoice_code, amount):
        """
        ثبت سند دریافت وجه پس از پرداخت آنلاین موفق (فقط شبیه‌سازی؛ قرارداد واقعی
        POST /Payment/ReciveFromCustomer در فاز سند دریافت).
        """
        if not self.config.write_is_mock:
            return self._write_disabled('register_payment')
        time.sleep(2.5)  # شبیه‌سازی تاخیر شبکه/زمان پردازش درخواست در هلو
        return {
            "success": True,
            "ReceiptCode": f"RCP_{invoice_code.split('_')[-1]}",
            "message": "سند دریافت وجه با موفقیت در حالت تست (Mock) ثبت شد"
        }
