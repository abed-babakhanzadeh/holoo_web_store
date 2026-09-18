"""
با هر تغییر مرتبط با سفارش (ثبت سفارش، تغییر وضعیت، افزودن/حذف ردیف)، رتبه‌بندی
«پرفروش‌ترین محصولات»/«دسته‌های پرفروش» صفحه اصلی که در products.home_cache کش شده
باطل می‌شود (چون این دو بر اساس Sum فروش واقعی‌شده‌ی OrderItem محاسبه می‌شوند).

جهت import (orders -> products) مجاز است چون products اپ پایه‌ی پروژه است (نگاه کنید
[[holoo-architecture-patterns]])؛ خودِ products هرگز از orders import نمی‌کند.
"""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from products.home_cache import clear_catalog_dependent_cache
from .models import Order, OrderItem


@receiver(post_save, sender=Order, dispatch_uid='home_cache_order_saved')
@receiver(post_save, sender=OrderItem, dispatch_uid='home_cache_order_item_saved')
@receiver(post_delete, sender=OrderItem, dispatch_uid='home_cache_order_item_deleted')
def invalidate_home_catalog_cache_on_order_change(sender, **kwargs):
    clear_catalog_dependent_cache()
