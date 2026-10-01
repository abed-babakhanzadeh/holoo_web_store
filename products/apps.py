from django.apps import AppConfig


class ProductsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'products'
    verbose_name = 'محصولات و مدیریت ویترین'

    def ready(self):
        # باطل‌کردن کش منوی دسته‌بندی/تنظیمات سایت پس از تغییر در ادمین
        from . import signals  # noqa: F401
        # ثبت تنظیمات وفاداری در رجیستری accounts.stats
        from . import stats  # noqa: F401

        # دکمه‌های ذخیره/ذخیره و ادامه/حذف در همه‌ی فرم‌های ادمین علاوه بر پایین، بالای فرم هم نمایش
        # داده شوند (فرم‌های بلند: لازم نباشد برای ذخیره تا انتها اسکرول کرد). پیش‌فرض کلاس پایه است،
        # پس هر ModelAdmin (حتی در اپ‌های دیگر) خودکار می‌گیرد؛ هرکدام بخواهد می‌تواند save_on_top = False بگذارد.
        from django.contrib import admin
        admin.ModelAdmin.save_on_top = True
