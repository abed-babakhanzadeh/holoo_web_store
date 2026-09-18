"""
سیگنال‌های دامنه‌ی products + باطل‌کردن کش داده‌های سراسری قالب.

منوی دسته‌بندی‌ها و تنظیمات فوتر برای ۱۵ دقیقه کش می‌شوند؛ این شنونده‌ها باعث می‌شوند
تغییر ادمین بلافاصله روی سایت دیده شود و لازم نباشد کسی منتظر انقضای کش بماند.
"""

import django.dispatch
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .context_processors import clear_storefront_cache
from .home_cache import clear_catalog_dependent_cache
from .models import Brand, Category, Product, SiteSettings

# اعلام می‌شود وقتی موجودی یک محصول از صفر/منفی به مثبت برسد (سینک هلو تشخیص می‌دهد،
# holoo/tasks.py send می‌کند). products نمی‌داند و لازم نیست بداند چه کسی به این رویداد
# گوش می‌دهد (اطلاع‌رسانی به کاربرهای منتظر، هر چیز دیگر در آینده).
# kwargs: product
product_back_in_stock = django.dispatch.Signal()


@receiver(post_save, sender=Category, dispatch_uid='storefront_cache_category_saved')
@receiver(post_delete, sender=Category, dispatch_uid='storefront_cache_category_deleted')
@receiver(post_save, sender=SiteSettings, dispatch_uid='storefront_cache_settings_saved')
def invalidate_storefront_cache(sender, **kwargs):
    clear_storefront_cache()


# کش شناسه‌های صفحه اصلی (جدیدترین/پرفروش‌ترین محصولات، دسته‌های پرفروش، برندهای محبوب -
# نگاه کنید products/home_cache.py) با تغییر محصول/برند/دسته باطل می‌شود؛ سهم سفارش‌ها
# (برای پرفروش‌ترین) در orders/home_cache_hooks.py است، چون products نباید از orders
# import کند.
@receiver(post_save, sender=Product, dispatch_uid='home_cache_product_saved')
@receiver(post_delete, sender=Product, dispatch_uid='home_cache_product_deleted')
@receiver(post_save, sender=Brand, dispatch_uid='home_cache_brand_saved')
@receiver(post_delete, sender=Brand, dispatch_uid='home_cache_brand_deleted')
@receiver(post_save, sender=Category, dispatch_uid='home_cache_category_saved')
@receiver(post_delete, sender=Category, dispatch_uid='home_cache_category_deleted')
def invalidate_home_catalog_cache(sender, **kwargs):
    clear_catalog_dependent_cache()
