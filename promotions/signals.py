"""
باطل‌سازی شاخص تخفیف‌ها با هر تغییر داده‌ی مؤثر بر آن: تخفیف، اهدافش، سیاست سراسری و درخت دسته‌ها.
(تغییر خودِ محصول نیاز به باطل‌سازی ندارد؛ شاخص فقط شناسه‌ی هدف‌ها را نگه می‌دارد و محصول را روی هر پرسش می‌خواند.)
"""

import logging

from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver

from orders.signals import order_canceled
from payments.signals import payment_succeeded
from products.models import Brand, Category

from . import coupons, free_shipping, index
from .models import DiscountPolicy, FreeShippingRule, Promotion, PromotionTarget

logger = logging.getLogger(__name__)


@receiver([post_save, post_delete], sender=Promotion, dispatch_uid='promotions_invalidate_promotion')
@receiver([post_save, post_delete], sender=PromotionTarget, dispatch_uid='promotions_invalidate_target')
@receiver([post_save, post_delete], sender=DiscountPolicy, dispatch_uid='promotions_invalidate_policy')
@receiver([post_save, post_delete], sender=Category, dispatch_uid='promotions_invalidate_category')
@receiver([post_delete], sender=Brand, dispatch_uid='promotions_invalidate_brand')
def invalidate_index(sender, **kwargs):
    index.invalidate()


# ---------------------------------------------------------------- قاعده‌های ارسال رایگان
@receiver([post_save, post_delete], sender=FreeShippingRule, dispatch_uid='promotions_invalidate_freeship_rule')
@receiver(m2m_changed, sender=FreeShippingRule.provinces.through, dispatch_uid='promotions_invalidate_freeship_provinces')
@receiver(m2m_changed, sender=FreeShippingRule.cities.through, dispatch_uid='promotions_invalidate_freeship_cities')
def invalidate_free_shipping(sender, **kwargs):
    # ادمین ابتدا خود قاعده را ذخیره و بعد استان/شهرها را ست می‌کند؛ پس m2m_changed هم لازم است
    free_shipping.invalidate()


# ---------------------------------------------------------------- چرخه‌ی عمر مصرف کد تخفیف
@receiver(payment_succeeded, dispatch_uid='promotions_redeem_coupon_on_payment')
def redeem_coupon_on_payment(sender, order, **kwargs):
    """ پرداخت موفق: رزرو کد این سفارش به «مصرف نهایی» تبدیل می‌شود """
    coupons.redeem_for_order(order.id)


@receiver(order_canceled, dispatch_uid='promotions_release_coupon_on_cancel')
def release_coupon_on_cancel(sender, order, **kwargs):
    """ لغو سفارش: ظرفیت کد آزاد می‌شود """
    coupons.release_for_order(order.id, 'order_canceled')
