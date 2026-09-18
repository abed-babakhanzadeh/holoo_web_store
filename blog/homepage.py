"""
ثبت تأمین‌کننده‌ی «آخرین مقالات» صفحه اصلی در رجیستری products.blog_posts + باطل‌کردن
کش آن با هر ذخیره/حذف مقاله.

جهت import (blog -> products) مجاز است چون products اپ پایه‌ی پروژه است (نگاه کنید
[[holoo-architecture-patterns]])؛ خودِ products هرگز از blog import نمی‌کند.
"""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from products.blog_posts import clear_cache, register_provider
from .models import Post


@register_provider
def get_latest_posts(limit):
    return list(Post.visible.select_related('category').order_by('-published_at')[:limit])


@receiver(post_save, sender=Post, dispatch_uid='home_cache_post_saved')
@receiver(post_delete, sender=Post, dispatch_uid='home_cache_post_deleted')
def invalidate_latest_posts_cache(sender, **kwargs):
    clear_cache()
