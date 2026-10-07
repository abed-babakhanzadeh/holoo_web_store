"""
اعتبارسنجی و پاک‌سازی تصویر آپلودی (لایه‌ی مشترک چت و مدارک چک؛ بدون وابستگی به هیچ اپ).

اصل: هیچ چیز از کاربر باور نمی‌شود — نه پسوند، نه Content-Type، نه نام فایل. نوع از بایت‌های ابتدایی (magic bytes) تشخیص داده
می‌شود و فقط JPEG، PNG و WebP مجازند. تصویر با Pillow کامل decode می‌شود (فایل خراب/ناقص/بمب فشرده‌سازی رد)، جهت EXIF اعمال
می‌شود و سپس **از نو** ذخیره می‌شود؛ پس EXIF/GPS، متادیتا، پروفایل رنگ و هر داده‌ی چسبیده به انتهای فایل (polyglot) از بین می‌رود.
تصویر بزرگ کوچک می‌شود؛ تصویر متحرک فقط فریم اول را نگه می‌دارد.
"""
import io

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_PIXELS = 40_000_000            # ~۴۰ مگاپیکسل؛ بیشتر ← احتمال بمب فشرده‌سازی
MAX_SIDE = 1920

# نوع تشخیص‌داده‌شده ← (Content-Type، پسوند)
IMAGE_TYPES = {'jpeg': ('image/jpeg', 'jpg'), 'png': ('image/png', 'png'), 'webp': ('image/webp', 'webp')}


class UnsafeUpload(ValueError):
    """ فایل پذیرفته نشد؛ code ماشینی و message فارسی برای نمایش به کاربر """

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def sniff_image(head):
    """ نوع واقعی تصویر از بایت‌های ابتدایی (حداقل ۱۲ بایت)، یا None اگر JPEG/PNG/WebP نیست """
    if head[:3] == b'\xff\xd8\xff':
        return 'jpeg'
    if head[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'webp'
    return None


def clean_image(data, detected, *, max_pixels=None, max_side=None):
    """
    بایت‌های تصویر ← (بایت‌های دوباره‌کدشده‌ی تمیز، عرض، ارتفاع) یا UnsafeUpload. detected خروجی sniff_image است؛ محتوای واقعی
    تصویر باید با آن بخواند. max_pixels/max_side برای override در تست یا کاربردهای خاص است.
    """
    max_pixels = MAX_PIXELS if max_pixels is None else max_pixels
    max_side = MAX_SIDE if max_side is None else max_side
    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.width * probe.height > max_pixels:
                raise UnsafeUpload('attachment_invalid', 'ابعاد تصویر بیش از حد بزرگ است.')
            if (probe.format or '').lower() != detected:
                raise UnsafeUpload('attachment_invalid', 'محتوای فایل با نوع آن نمی‌خواند.')
            probe.seek(0)
            image = probe.copy()
            image.load()                                    # decode کامل؛ فایل خراب/ناقص همین‌جا خطا می‌دهد
    except UnsafeUpload:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError, EOFError):
        raise UnsafeUpload('attachment_invalid', 'فایل تصویر معتبر نیست.')
    image = ImageOps.exif_transpose(image)
    if max(image.size) > max_side:
        image.thumbnail((max_side, max_side), Image.LANCZOS)
    out = io.BytesIO()
    if detected == 'jpeg':
        image.convert('RGB').save(out, 'JPEG', quality=88, optimize=True)
    elif detected == 'png':
        image.convert('RGBA' if 'A' in image.getbands() or image.mode in ('P', 'LA') and 'transparency' in image.info else 'RGB') \
            .save(out, 'PNG', optimize=True)
    else:
        image.convert('RGBA' if 'A' in image.getbands() else 'RGB').save(out, 'WEBP', quality=85, method=4)
    return out.getvalue(), image.width, image.height
