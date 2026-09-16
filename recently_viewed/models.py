from django.db import models
from accounts.models import CustomUser
from products.models import Product

MAX_ITEMS_PER_USER = 24


class RecentlyViewed(models.Model):
    """ تاریخچه‌ی بازدید محصولات هر کاربر """
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='recently_viewed', verbose_name='کاربر')
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='recently_viewed_by', verbose_name='محصول')
    viewed_at = models.DateTimeField(auto_now=True, verbose_name='آخرین بازدید')

    class Meta:
        verbose_name = 'بازدید اخیر'
        verbose_name_plural = 'بازدیدهای اخیر'
        unique_together = ('user', 'product')
        ordering = ('-viewed_at',)

    def __str__(self):
        return f"{self.user.phone_number} - {self.product.name}"

    @classmethod
    def track(cls, user, product):
        """
        ثبت/به‌روزرسانی بازدید و حذف قدیمی‌ترین موارد اضافه بر سقف مجاز.

        پاک‌سازی فقط وقتی انجام می‌شود که ردیف تازه ساخته شده باشد: با صرفاً به‌روز شدن
        زمانِ یک ردیف موجود، تعداد کل تغییر نمی‌کند و اجرای کوئری پاک‌سازی روی هر بازدید
        (که در عمل اکثر بازدیدها هستند) دو کوئری بی‌فایده به هر صفحه‌ی محصول اضافه می‌کرد.
        """
        obj, created = cls.objects.update_or_create(user=user, product=product)
        if created:
            stale_ids = list(
                cls.objects.filter(user=user).order_by('-viewed_at')
                .values_list('id', flat=True)[MAX_ITEMS_PER_USER:]
            )
            if stale_ids:
                cls.objects.filter(id__in=stale_ids).delete()
        return obj
