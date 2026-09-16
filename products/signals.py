"""
باطل‌کردن کش داده‌های سراسری قالب.

منوی دسته‌بندی‌ها و تنظیمات فوتر برای ۱۵ دقیقه کش می‌شوند؛ این شنونده‌ها باعث می‌شوند
تغییر ادمین بلافاصله روی سایت دیده شود و لازم نباشد کسی منتظر انقضای کش بماند.
"""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .context_processors import clear_storefront_cache
from .models import Category, SiteSettings


@receiver(post_save, sender=Category, dispatch_uid='storefront_cache_category_saved')
@receiver(post_delete, sender=Category, dispatch_uid='storefront_cache_category_deleted')
@receiver(post_save, sender=SiteSettings, dispatch_uid='storefront_cache_settings_saved')
def invalidate_storefront_cache(sender, **kwargs):
    clear_storefront_cache()
