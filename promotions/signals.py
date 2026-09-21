"""
باطل‌سازی شاخص تخفیف‌ها با هر تغییر داده‌ی مؤثر بر آن: تخفیف، اهدافش، سیاست سراسری و درخت دسته‌ها.
(تغییر خودِ محصول نیاز به باطل‌سازی ندارد؛ شاخص فقط شناسه‌ی هدف‌ها را نگه می‌دارد و محصول را روی هر پرسش می‌خواند.)
"""

import logging

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from products.models import Brand, Category

from . import index
from .models import DiscountPolicy, Promotion, PromotionTarget

logger = logging.getLogger(__name__)


@receiver([post_save, post_delete], sender=Promotion, dispatch_uid='promotions_invalidate_promotion')
@receiver([post_save, post_delete], sender=PromotionTarget, dispatch_uid='promotions_invalidate_target')
@receiver([post_save, post_delete], sender=DiscountPolicy, dispatch_uid='promotions_invalidate_policy')
@receiver([post_save, post_delete], sender=Category, dispatch_uid='promotions_invalidate_category')
@receiver([post_delete], sender=Brand, dispatch_uid='promotions_invalidate_brand')
def invalidate_index(sender, **kwargs):
    index.invalidate()
