"""
ذخیره‌ی پیوست‌های گفتگو: MEDIA_ROOT/chat_attachments/ (مسیر از settings.MEDIA_ROOT در لحظه‌ی استفاده خوانده می‌شود، پس تست‌ها با
override_settings(MEDIA_ROOT=پوشه‌ی موقت) هرگز به media/ واقعی دست نمی‌زنند). فایل‌ها عمداً هیچ نشانی عمومی ندارند (url() خطا می‌دهد):
فقط از راه ویوهای دارای کنترل دسترسی (chat.api.file_view / chat.console.file_api) سرو می‌شوند.
"""
import os

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.utils.functional import cached_property


class ChatAttachmentStorage(FileSystemStorage):
    @cached_property
    def base_location(self):
        return os.path.join(str(settings.MEDIA_ROOT), 'chat_attachments')

    @cached_property
    def location(self):
        return os.path.abspath(self.base_location)

    def url(self, name):
        raise ValueError('پیوست‌های گفتگو نشانی عمومی ندارند؛ از ویوی دانلود دارای کنترل دسترسی استفاده کنید.')


chat_attachment_storage = ChatAttachmentStorage()
