import base64
import json
import logging
import time
from datetime import timedelta

import requests

from .conf import get_config
from .invoice import payload_total
from .wire import (CODE_DUPLICATE_CLIENT_ID, CODES_EXISTING_CUSTOMER, comment_mentions_order, customer_body,
                   customer_update_body, invoice_body, parse_response)

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

    def get_json(self, path, params=None, timeout=None):
        """ GET عمومی و فقط‌خواندنی برای ابزارهای تشخیصی (مثل /Version، /Settings). در حالت mock همیشه None. """
        if self.products_mock:
            return None
        return self._authenticated_get(f"{self.base_url}/{path.lstrip('/')}", params=params, timeout=timeout)

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

    # ------------------------------------------------------------------ نوشتن
    #
    # سه حالت (holoo/conf.py): mock = شبیه‌سازی محلی؛ disabled = رد صریح بدون هیچ تماس؛ real = ارسال واقعی، فقط وقتی
    # HOLOO_WRITE_MODE=real و نام دیتابیس در HOLOO_WRITE_ALLOWED_DBS (پیش‌فرض فقط Holoo2) باشد.
    # خروجی همه‌ی متدها دیکشنری {'success': bool, ...}؛ در شکست: 'code' (ErrorCode هلو)، 'message' و 'transient'
    # (True = تلاش دوباره معنی دارد؛ False = خطای دائمی داده که باید اصلاح شود).

    def _write_disabled(self, operation):
        logger.warning("Holoo %s رد شد: HOLOO_WRITE_MODE=disabled (هیچ درخواستی به هلو فرستاده نشد).", operation)
        return {
            "success": False, "disabled": True, "transient": False, "code": "WRITE_DISABLED",
            "message": "نوشتن در هلو غیرفعال است (HOLOO_WRITE_MODE=disabled).",
        }

    def _real_write(self, method, path, body, params=None):
        """
        تنها دروازه‌ی نوشتن واقعی. خروجی parse_response(...) یا شکستِ «موقت» برای خطای شبکه/لاگین/HTTP 5xx.
        هیچ‌چیز این‌جا استثنا نمی‌دهد: تصمیمِ retry با فراخوان‌کننده است.
        """
        assert method in ('POST', 'PUT'), method
        # دفاع دوم (conf.py دفاع اول است): هرگز بیرون از حالت real و فهرست سفید دیتابیس چیزی نوشته نمی‌شود
        allowed = {name.lower() for name in self.config.write_allowed_dbs}
        if not self.config.write_is_real or self.config.db_name.lower() not in allowed:
            return self._write_disabled(f'{method} {path}')

        url = f"{self.base_url}/{path.lstrip('/')}"
        payload = json.dumps(body, ensure_ascii=False).encode('utf-8')
        response = None
        for attempt in (1, 2):
            try:
                headers = {"Authorization": self._get_auth_header(), "Content-Type": "application/json; charset=utf-8"}
            except RuntimeError as error:
                return {"success": False, "code": "LOGIN_FAILED", "message": str(error), "transient": True}
            try:
                send = requests.post if method == 'POST' else requests.put
                response = send(url, data=payload, headers=headers, params=params, timeout=self.config.timeout)
            except requests.RequestException as error:
                logger.error("Holoo %s %s failed: %s", method, path, error)
                return {"success": False, "code": "NETWORK", "message": str(error), "transient": True}
            if response.status_code == 401 and attempt == 1:
                _clear_token_cache()                       # توکن منقضی شده؛ یک‌بار دوباره لاگین
                continue
            break

        if response.status_code >= 500:
            return {"success": False, "code": f"HTTP_{response.status_code}", "message": response.text[:300], "transient": True}
        if response.status_code >= 400:
            return {"success": False, "code": f"HTTP_{response.status_code}", "message": response.text[:300], "transient": False}
        try:
            data = response.json()
        except ValueError:
            data = None
        return parse_response(data)

    def _lookup_customer(self, **filters):
        """ ردیف مشتری از هلو با یکی از فیلترهای erpcode / webid / mobile (برای پذیرش مشتریِ موجود) یا None """
        data = self.get_json('Customer', params=filters)
        rows = data.get('Customer', []) if isinstance(data, dict) else []
        if 'erpcode' in filters:
            rows = [row for row in rows if row.get('ErpCode') == filters['erpcode']]
        elif 'webid' in filters:
            rows = [row for row in rows if str(row.get('WebId')) == str(filters['webid'])]
        return rows[0] if rows else None

    def _client_id(self, raw):
        """ id سمت کلاینت = پیشوند اختیاری (HOLOO_CLIENT_ID_PREFIX) + شناسه‌ی سایت. در هلو یکتاست؛ پیشوند از برخورد
        شماره‌ی سفارش/کاربرِ دیتابیس‌های توسعه و تست با دیتابیس اصلی جلوگیری می‌کند. """
        return f"{self.config.client_id_prefix}{raw}"

    def insert_person(self, first_name, last_name, phone_number, national_code, address=None, *, web_id=None,
                      province='', city='', postal_code='', email=''):
        """
        ثبت مشتری جدید در هلو (POST /Customer). خروجی موفق: erp_code، code (کد طرف‌حساب)، bed_sarfasl.

        اگر موبایل یا کدملی در هلو تکراری باشد (خطای ۱۰/۲۳) هلو ErpCode مشتریِ موجود را برمی‌گرداند و همان «پذیرفته»
        می‌شود (adopted=True): هم تکرارِ بعد از گم‌شدن پاسخ (که مشتریِ خودمان است) را بی‌خطر می‌کند، هم مشتریِ قدیمیِ
        فروشگاه را به حساب سایت وصل می‌کند.
        """
        if self.config.write_is_mock:
            time.sleep(2)
            return {"success": True, "erp_code": f"ERP_{phone_number[-4:]}", "code": f"C{phone_number[-4:]}",
                    "bed_sarfasl": "1030000", "adopted": False, "message": "شخص با موفقیت ثبت شد"}
        if not self.config.write_is_real:
            return self._write_disabled('insert_person')

        web_id = self._client_id(web_id if web_id is not None else phone_number)
        body = customer_body(web_id=web_id, first_name=first_name, last_name=last_name,
                             phone_number=phone_number, national_code=national_code, province=province, city=city,
                             address=address, postal_code=postal_code, email=email)
        result = self._real_write('POST', 'Customer', body)
        if result.get('success'):
            data = result['data']
            return {"success": True, "erp_code": data.get('ErpCode'), "code": data.get('Code'),
                    "bed_sarfasl": data.get('BedSarfasl'), "adopted": False, "message": "شخص با موفقیت ثبت شد"}

        if result.get('code') == CODE_DUPLICATE_CLIENT_ID:
            # id سمت کلاینت (WebId) قبلاً در هلو ثبت شده: ثبتِ قبلیِ همین کاربر که پاسخش گم شده است ← همان را بپذیر
            row = self._lookup_customer(webid=web_id)
            if row is None:
                return {**result, "message": "شناسه‌ی کاربر در هلو تکراری است ولی مشتری‌اش پیدا نشد؛ بررسی دستی لازم است."}
            return {"success": True, "erp_code": row.get('ErpCode'), "code": row.get('Code'), "bed_sarfasl": row.get('BedSarfasl'),
                    "adopted": True, "message": "مشتری موجود در هلو پذیرفته شد"}

        existing = (result.get('failure') or {}).get('ExistingCustomerErpCode')
        if result.get('code') in CODES_EXISTING_CUSTOMER and existing:
            row = self._lookup_customer(erpcode=existing)
            if row is None:
                return {**result, "transient": True, "message": "مشتری موجود در هلو پیدا نشد؛ دوباره تلاش می‌شود."}
            logger.warning("مشتری موبایل %s از قبل در هلو بود (کد %s)؛ به همان وصل شد.", phone_number, row.get('Code'))
            return {"success": True, "erp_code": existing, "code": row.get('Code'), "bed_sarfasl": row.get('BedSarfasl'),
                    "adopted": True, "message": "مشتری موجود در هلو پذیرفته شد"}
        return result

    def update_person(self, erp_code, first_name=None, last_name=None, address=None, *, web_id=None, phone_number=None,
                      national_code=None, province=None, city=None, postal_code=None, email=None, **kwargs):
        """ ویرایش مشتری موجود (PUT /Customer)؛ فقط فیلدهای داده‌شده عوض می‌شوند """
        if self.config.write_is_mock:
            time.sleep(1)
            return {"success": True, "message": "اطلاعات شخص با موفقیت به‌روز شد"}
        if not self.config.write_is_real:
            return self._write_disabled('update_person')

        body = customer_update_body(erp_code, web_id=self._client_id(web_id) if web_id is not None else None, first_name=first_name, last_name=last_name, phone_number=phone_number,
                                    national_code=national_code, province=province, city=city, address=address,
                                    postal_code=postal_code, email=email)
        result = self._real_write('PUT', 'Customer', body)
        if result.get('success'):
            return {"success": True, "message": "اطلاعات شخص به‌روز شد"}
        return result

    def find_invoice_for_order(self, order_id, customer_erp_code, around, expected_total=None):
        """
        فاکتورِ ثبت‌شده‌ی یک سفارش سایت در هلو یا None: بعد از خطای ۱۰۲ (شناسه‌ی تکراری) یعنی فاکتور قبلاً ساخته شده ولی
        پاسخش به ما نرسیده. هلو جستجو با id کلاینت را پشتیبانی نمی‌کند، پس لیست فاکتورهای فروشِ همان روزها خوانده و با
        کامنت («سفارش آنلاین سایت کد #<id>») و مشتری تطبیق داده می‌شود.
        """
        day = timedelta(days=1)
        data = self.get_json('Invoice/Invoice', params={
            'type': 2,                                     # در فهرست، فروش = ۲ (در ثبت = ۱)
            'date.from': (around - day).strftime('%Y-%m-%d'), 'date.to': (around + 3 * day).strftime('%Y-%m-%d'),
        }, timeout=max(self.config.timeout, 90))
        rows = data.get('invoice', []) if isinstance(data, dict) else []
        matches = [row for row in rows
                   if row.get('CustomerErpCode') == customer_erp_code and comment_mentions_order(row.get('comment'), order_id)]
        if expected_total is not None:
            exact = [row for row in matches if abs(float(row.get('SumPrice') or 0) - float(expected_total)) < 0.5]
            matches = exact or matches
        if len(matches) > 1:
            # نباید پیش بیاید (id یکتاست)؛ اگر آمد یعنی فاکتور تکراری در هلو هست. قدیمی‌ترین را می‌پذیریم و بلند گزارش می‌کنیم.
            logger.error("چند فاکتور برای سفارش %s در هلو پیدا شد (%s)؛ قدیمی‌ترین پذیرفته شد، بررسی دستی لازم است.",
                         order_id, [row.get('Code') for row in matches])
        return min(matches, key=lambda row: int(row.get('Code') or 0)) if matches else None

    def insert_invoice(self, payload):
        """
        ثبت فاکتور فروش قطعی (POST /Invoice/Invoice) از روی ساختار خنثی (holoo/invoice.py).
        طبق تصمیم کارفرما پیش‌فاکتور ثبت نمی‌شود و فاکتور فقط پس از «تأیید مدیر» صادر می‌شود.

        id سمت کلاینت = شماره‌ی سفارش و در هلو یکتاست؛ تکرار (خطای ۱۰۲) یعنی فاکتور قبلاً ساخته شده و فاکتور موجود
        پذیرفته می‌شود (adopted=True) نه اینکه دوباره ثبت شود. خروجی موفق: InvoiceCode (شماره‌ی فاکتور)، ErpCode، SanadCode.
        """
        if self.config.write_is_mock:
            import random
            time.sleep(1.5)  # شبیه‌سازی تاخیر شبکه
            return {"success": True, "InvoiceCode": f"INV_{random.randint(10000, 99999)}", "ErpCode": None, "SanadCode": None,
                    "adopted": False, "message": "فاکتور با موفقیت در حالت تست (Mock) ثبت شد"}
        if not self.config.write_is_real:
            return self._write_disabled('insert_invoice')

        result = self._real_write('POST', 'Invoice/Invoice', invoice_body(payload, self.config.client_id_prefix))
        if result.get('success'):
            data = result['data']
            return {"success": True, "InvoiceCode": str(data.get('Code')), "ErpCode": data.get('ErpCode'),
                    "SanadCode": str(data.get('SanadCode') or ''), "adopted": False, "message": "فاکتور در هلو ثبت شد"}

        if result.get('code') == CODE_DUPLICATE_CLIENT_ID:
            row = self.find_invoice_for_order(payload['OrderId'], payload['CustomerErpCode'], payload['IssuedAt'],
                                              expected_total=payload_total(payload))
            if row is not None:
                logger.warning("فاکتور سفارش %s از قبل در هلو بود (شماره %s)؛ همان پذیرفته شد.", payload['OrderId'], row.get('Code'))
                return {"success": True, "InvoiceCode": str(row.get('Code')), "ErpCode": row.get('ErpCode'),
                        "SanadCode": str(row.get('SanadCode') or ''), "adopted": True, "message": "فاکتور موجود در هلو پذیرفته شد"}
            return {**result, "message": "شناسه‌ی سفارش در هلو تکراری است ولی فاکتورش پیدا نشد؛ بررسی دستی لازم است."}
        return result

    def register_payment(self, invoice_code, amount):
        """
        سند دریافت وجه جدا (POST /Payment/ReciveFromCustomer). در جریان واقعی لازم نیست: فاکتورِ سفارش پرداخت‌شده با همان
        کارتخوان تسویه‌شده ثبت می‌شود (holoo/wire.py::invoice_body). چون id سند دریافت در هلو یکتا نیست (ارسال دوباره = سند
        دوم)، ارسال واقعی عمداً پیاده نشده است.
        """
        if self.config.write_is_mock:
            time.sleep(2.5)  # شبیه‌سازی تاخیر شبکه/زمان پردازش درخواست در هلو
            return {
                "success": True,
                "ReceiptCode": f"RCP_{invoice_code.split('_')[-1]}",
                "message": "سند دریافت وجه با موفقیت در حالت تست (Mock) ثبت شد"
            }
        if not self.config.write_is_real:
            return self._write_disabled('register_payment')
        return {"success": False, "code": "NOT_IMPLEMENTED", "transient": False,
                "message": "سند دریافت جدا در حالت real پیاده نشده؛ فاکتور پرداخت‌شده با کارتخوان تسویه می‌شود."}
