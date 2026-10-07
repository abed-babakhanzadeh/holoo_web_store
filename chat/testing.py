"""ابزار مشترک تست‌های چت (ساختن کاربر/کارشناس، تنظیمات، گفتگو)."""
import shutil
import tempfile
import uuid

from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from accounts.models import CustomUser
from chat import cache as chatcache
from chat import conversations as conv
from chat.apps import OPERATOR_GROUP_NAME, ensure_operator_group
from chat.models import ChatMessage, Conversation
from products.chat_settings import CHAT_CFG_CACHE_KEY
from products.models import SiteSettings


class ChatTestBase(TestCase):
    """ چت روشن، Redis تمیز، مدارشکن بسته؛ هر تست از صفر """

    def setUp(self):
        # پیوست‌های تست هرگز به media/ واقعی نمی‌روند: MEDIA_ROOT برای هر تست یک پوشه‌ی موقت است
        self.media_root = tempfile.mkdtemp(prefix='chat-media-')
        self.addCleanup(shutil.rmtree, self.media_root, True)
        self.enterContext(override_settings(MEDIA_ROOT=self.media_root))
        cache.clear()
        chatcache.reset_breaker()
        self.addCleanup(cache.clear)
        self.addCleanup(chatcache.reset_breaker)
        self.set(chat_enabled=True, chat_hours_mode='always')
        self._phone = 9120000100

    def set(self, **values):
        SiteSettings.objects.update_or_create(pk=1, defaults=values)
        cache.delete(SiteSettings.CACHE_KEY)
        cache.delete(CHAT_CFG_CACHE_KEY)

    def make_user(self, **extra):
        self._phone += 1
        return CustomUser.objects.create_user(f'0{self._phone}', **extra)

    def make_operator(self, *, superuser=False, **extra):
        if superuser:
            self._phone += 1
            return CustomUser.objects.create_superuser(f'0{self._phone}', password='x')
        user = self.make_user(is_staff=True, first_name='کارشناس', **extra)
        ensure_operator_group(sender=None)
        user.groups.add(Group.objects.get(name=OPERATOR_GROUP_NAME))
        return user

    def new_conversation(self, *, user=None, body='سلام', visitor='v' * 40, name='مهمان تست', phone='', status=None, **updates):
        """ گفتگوی آفلاین تازه با اولین پیام (T2) از مسیر واقعی دامنه """
        conversation, message = conv.create_offline_conversation(
            user=user, visitor_hash=visitor, name=name, phone=phone, body=body,
            client_msg_id=uuid.uuid4(), source_path='/shop/', ip='1.2.3.0')
        if status or updates:
            fields = dict(updates)
            if status:
                fields['status'] = status
            Conversation.objects.filter(pk=conversation.pk).update(**fields)
            conversation.refresh_from_db()
        return conversation

    def customer_says(self, conversation, body='پیام مشتری', **kw):
        return conv.post_message(conversation, sender=ChatMessage.SENDER_CUSTOMER, body=body, **kw)[0]

    def operator_says(self, conversation, operator, body='پاسخ کارشناس', **kw):
        return conv.post_message(conversation, sender=ChatMessage.SENDER_OPERATOR, body=body, operator=operator, **kw)[0]

    def reload(self, conversation):
        return Conversation.objects.select_related('user', 'assigned_operator').get(pk=conversation.pk)


def guest_client():
    return Client()


# ------------------------------------------------------------------ نمونه فایل برای تست پیوست

def make_image(fmt='JPEG', size=(40, 30), *, exif=False, orientation=None, trailing=b'', color=(200, 40, 40)):
    """ بایت‌های یک تصویر واقعی؛ exif=True متادیتای شناسایی‌پذیر (SECRET-GPS-123) می‌گذارد، trailing داده‌ی چسبیده به انتهای فایل """
    import io

    from PIL import Image

    image = Image.new('RGB', size, color)
    buffer = io.BytesIO()
    kwargs = {}
    if fmt == 'JPEG' and (exif or orientation):
        tags = Image.Exif()
        if exif:
            tags[0x010E] = 'SECRET-GPS-123'
            tags[0x010F] = 'SECRET-CAMERA'
        if orientation:
            tags[0x0112] = orientation
        kwargs['exif'] = tags.tobytes()
    image.save(buffer, fmt, **kwargs)
    return buffer.getvalue() + trailing


def make_pdf(extra=b'', eof=True):
    body = b'%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n' + extra + b'\ntrailer\n<< /Root 1 0 R >>\n'
    return body + (b'%%EOF\n' if eof else b'')


def upload(data, name='photo.jpg', content_type='image/jpeg'):
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile(name, data, content_type=content_type)
