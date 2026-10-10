import os

from django.core.files.storage import FileSystemStorage


class VersionedFileSystemStorage(FileSystemStorage):
    """
    FileSystemStorage که به نشانی هر فایل «نسخه» (زمان آخرین تغییر فایل) را اضافه می‌کند: /media/x.jpg?v=1760000000.

    چرا لازم است: همگام‌سازی تصاویر از پوشه‌ی کاتالوگ بر اساس «نام فایل» کار می‌کند. وقتی عکاس عکسِ هم‌نام را
    جایگزین می‌کند نشانیِ تصویر عوض نمی‌شود، پس مرورگر (و CDN) نسخه‌ی قبلی را از کش نشان می‌داد. با این نسخه،
    هر بار که محتوای فایل عوض شود نشانی هم عوض می‌شود و عکس تازه بدون نیاز به پاک‌کردن کش دیده می‌شود.
    همه‌ی استفاده‌های `.url` (قالب‌ها، کارت محصول، سبد، گالری) خودکار پوشش داده می‌شوند.
    """

    def url(self, name):
        url = super().url(name)
        try:
            version = int(os.path.getmtime(self.path(name)))
        except (OSError, ValueError, TypeError):
            return url   # فایل نبود: همان نشانی ساده (۴۰۴ سمت وب‌سرور، بدون خطا در رندر)
        return f'{url}?v={version}'


def versioned_media_storage():
    """ callable برای storage= فیلدها؛ در مایگریشن به‌صورت ارجاع به این تابع ثبت می‌شود """
    return VersionedFileSystemStorage()
