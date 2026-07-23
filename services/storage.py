from django.core.files.storage import FileSystemStorage


class OverwriteStorage(FileSystemStorage):
    """ به‌جای پسوندگذاری خودکار روی نام تکراری، فایل قدیمی را overwrite می‌کند """

    def get_available_name(self, name, max_length=None):
        if self.exists(name):
            self.delete(name)
        return name
