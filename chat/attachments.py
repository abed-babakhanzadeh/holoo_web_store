"""
پیوست‌های گفتگو. اصل: **هیچ چیز از کاربر باور نمی‌شود** — نه پسوند، نه Content-Type، نه نام فایل. نوع از بایت‌های ابتدای فایل
(magic bytes) تشخیص داده می‌شود و فقط این چهار نوع مجازند: JPEG، PNG، WebP، و (طبق تنظیم) PDF. هر چیز دیگر (SVG، ZIP، اجرایی،
HTML، ...) رد می‌شود.

  تصویر: با Pillow کامل decode می‌شود (فایل خراب/ناقص/بمب فشرده‌سازی رد)، جهت EXIF اعمال می‌شود، سپس **از نو** ذخیره می‌شود؛ پس
         EXIF/GPS، متادیتا، پروفایل رنگ و هر داده‌ی چسبیده به انتهای فایل (polyglot) از بین می‌رود. تصویر بزرگ به ۱۹۲۰ پیکسل کوچک
         می‌شود. فریم اول برای تصویر متحرک.
  PDF:   سرآیند %PDF-، پایان %%EOF، بدون رمز، و نبود نشانه‌های فعال (JavaScript/Launch/EmbeddedFile/OpenAction/AA/RichMedia/XFA/...)
         حتی با نام‌های هگز-رمزشده (/J#61vaScript). محدودیت شناخته‌شده: اگر شیء‌ها داخل ObjStm فشرده باشند اسکن خام آن‌ها را
         نمی‌بیند؛ به همین دلیل PDF هرگز inline نمایش داده نمی‌شود، همیشه دانلودی با CSP sandbox و nosniff است.
  نام فایل: فقط برای نمایش (پاک‌سازی‌شده، ۸۰ نویسه)؛ مسیر ذخیره uuid و پسوندِ نوعِ واقعی است.
"""
import io
import re
from dataclasses import dataclass

from django.http import FileResponse
from PIL import Image, ImageOps, UnidentifiedImageError

from .conversations import ChatError
from .text import safe_inline

MAX_PIXELS = 40_000_000            # ~۴۰ مگاپیکسل؛ بیشتر ← احتمال بمب فشرده‌سازی
MAX_SIDE = 1920
MAX_PER_CONVERSATION = 60
CODES = {'jpeg': ('image', 'image/jpeg', 'jpg'), 'png': ('image', 'image/png', 'png'), 'webp': ('image', 'image/webp', 'webp'),
         'pdf': ('pdf', 'application/pdf', 'pdf')}

PDF_FORBIDDEN = (b'/JavaScript', b'/JS', b'/Launch', b'/EmbeddedFile', b'/OpenAction', b'/AA', b'/RichMedia', b'/XFA',
                 b'/SubmitForm', b'/ImportData', b'/GoToR', b'/GoToE', b'/Encrypt')
_HEX_ESCAPE = re.compile(rb'#([0-9A-Fa-f]{2})')


@dataclass
class Prepared:
    data: bytes
    kind: str
    content_type: str
    ext: str
    name: str
    width: int = None
    height: int = None


def sniff(head):
    """ نوع واقعی از بایت‌های ابتدایی؛ None اگر مجاز نیست """
    if head[:3] == b'\xff\xd8\xff':
        return 'jpeg'
    if head[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'webp'
    if head[:5] == b'%PDF-':
        return 'pdf'
    return None


def display_name(original):
    name = str(original or '').replace('\\', '/').rsplit('/', 1)[-1]
    name = re.sub(r'[\x00-\x1f\x7f<>:"|?*]', '', name)
    return safe_inline(name, 80) or 'فایل'


def allowed_kinds(cfg):
    return {'image', 'pdf'} if cfg.chat_attachments_mode == 'images_docs' else {'image'}


def accept_attribute(cfg):
    return 'image/jpeg,image/png,image/webp' + (',application/pdf' if cfg.chat_attachments_mode == 'images_docs' else '')


def _clean_image(data, detected):
    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.width * probe.height > MAX_PIXELS:
                raise ChatError('attachment_invalid', 'ابعاد تصویر بیش از حد بزرگ است.')
            if (probe.format or '').lower() != detected:
                raise ChatError('attachment_invalid', 'محتوای فایل با نوع آن نمی‌خواند.')
            probe.seek(0)
            image = probe.copy()
            image.load()                                    # decode کامل؛ فایل خراب/ناقص همین‌جا خطا می‌دهد
    except ChatError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError, EOFError):
        raise ChatError('attachment_invalid', 'فایل تصویر معتبر نیست.')
    image = ImageOps.exif_transpose(image)
    if max(image.size) > MAX_SIDE:
        image.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    out = io.BytesIO()
    if detected == 'jpeg':
        image.convert('RGB').save(out, 'JPEG', quality=88, optimize=True)
    elif detected == 'png':
        image.convert('RGBA' if 'A' in image.getbands() or image.mode in ('P', 'LA') and 'transparency' in image.info else 'RGB') \
            .save(out, 'PNG', optimize=True)
    else:
        image.convert('RGBA' if 'A' in image.getbands() else 'RGB').save(out, 'WEBP', quality=85, method=4)
    return out.getvalue(), image.width, image.height


def _check_pdf(data):
    if b'%%EOF' not in data[-2048:]:
        raise ChatError('attachment_invalid', 'فایل PDF کامل نیست.')
    plain = _HEX_ESCAPE.sub(lambda m: bytes([int(m.group(1), 16)]), data)
    for token in PDF_FORBIDDEN:
        if token in plain:
            raise ChatError('attachment_invalid', 'این فایل PDF حاوی محتوای فعال یا رمزگذاری است و پذیرفته نمی‌شود.')


def prepare_uploads(files, cfg):
    """ فایل‌های آپلودشده (UploadedFile) ← فهرست Prepared اعتبارسنجی‌شده/پاک‌سازی‌شده، یا ChatError """
    files = [f for f in files if f is not None]
    if not files:
        return []
    if not cfg.chat_attachments_enabled:
        raise ChatError('attachment_disabled', 'ارسال پیوست فعال نیست.', 403)
    if len(files) > int(cfg.chat_attachment_max_count):
        raise ChatError('attachment_count', f'حداکثر {int(cfg.chat_attachment_max_count)} فایل در هر پیام مجاز است.')
    limit = int(cfg.chat_attachment_max_mb) * 1024 * 1024
    kinds = allowed_kinds(cfg)
    result = []
    for upload in files:
        if upload.size > limit:
            raise ChatError('attachment_size', f'حجم هر فایل حداکثر {int(cfg.chat_attachment_max_mb)} مگابایت است.')
        data = upload.read(limit + 1)
        if len(data) > limit:
            raise ChatError('attachment_size', f'حجم هر فایل حداکثر {int(cfg.chat_attachment_max_mb)} مگابایت است.')
        detected = sniff(data[:16])
        if detected is None or CODES[detected][0] not in kinds:
            raise ChatError('attachment_type', 'نوع فایل مجاز نیست.' if detected is None else 'ارسال PDF مجاز نیست.')
        kind, content_type, ext = CODES[detected]
        width = height = None
        if kind == 'image':
            data, width, height = _clean_image(data, detected)
        else:
            _check_pdf(data)
        result.append(Prepared(data=data, kind=kind, content_type=content_type, ext=ext, name=display_name(upload.name),
                               width=width, height=height))
    return result


def file_response(attachment):
    """ پاسخ دانلود امن: تصویر inline (دوباره‌کدشده)، PDF همیشه دانلودی؛ در هر دو nosniff و CSP sandbox """
    try:
        handle = attachment.file.open('rb')
    except (FileNotFoundError, ValueError, OSError):
        return None
    response = FileResponse(handle, content_type=attachment.content_type)
    disposition = 'inline' if attachment.kind == 'image' else 'attachment'
    safe = re.sub(r'[^A-Za-z0-9._-]', '_', f'{attachment.public_id}.{attachment.ext}')
    response['Content-Disposition'] = f'{disposition}; filename="{safe}"'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Content-Security-Policy'] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox"
    response['Cache-Control'] = 'private, max-age=3600'
    return response


def payload(message, url_for):
    """ فهرست پیوست‌های یک پیام برای JSON (url_for(attachment) ← نشانی دانلود دارای کنترل دسترسی) """
    items = []
    if not message.attachments_count:
        return items
    for a in message.attachments.all():
        items.append({'id': str(a.public_id), 'kind': a.kind, 'name': a.original_name, 'size': a.size, 'type': a.content_type,
                      'url': url_for(a), 'width': a.width, 'height': a.height})
    return items
