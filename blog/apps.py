from django.apps import AppConfig


class BlogConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'blog'
    verbose_name = 'وبلاگ'

    def ready(self):
        # ثبت تأمین‌کننده‌ی «آخرین مقالات» در رجیستری صفحه اصلی (products.blog_posts)
        from . import homepage  # noqa: F401
