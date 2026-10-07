"""
ذخیره‌ی خصوصی زیر MEDIA_ROOT/<subdir>/ بدون هیچ نشانی عمومی (url() خطا می‌دهد): فایل‌ها فقط از ویوهای دارای کنترل دسترسی سرو
می‌شوند. مسیر از settings.MEDIA_ROOT در لحظه‌ی استفاده خوانده می‌شود، پس تست‌ها با override_settings(MEDIA_ROOT=پوشه‌ی موقت)
هرگز به media/ واقعی دست نمی‌زنند. هر کاربرد یک زیرکلاس با subdir خودش می‌سازد (نگاه کنید orders/storage.py).
"""
import os

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.utils.functional import cached_property


class PrivateMediaStorage(FileSystemStorage):
    subdir = ''

    @cached_property
    def base_location(self):
        return os.path.join(str(settings.MEDIA_ROOT), self.subdir)

    @cached_property
    def location(self):
        return os.path.abspath(self.base_location)

    def url(self, name):
        raise ValueError('این فایل نشانی عمومی ندارد؛ از ویوی دانلود دارای کنترل دسترسی استفاده کنید.')
