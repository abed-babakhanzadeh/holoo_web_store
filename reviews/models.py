from django.db import models
from django.db.models import Q
from accounts.models import CustomUser
from products.models import Product, ProductColor


def _is_verified_purchase(user, product):
    """ آیا کاربر این محصول را در یک سفارش با پرداخت موفق خریده است """
    from orders.models import OrderItem
    return OrderItem.objects.filter(
        product=product, order__user=user, order__transactions__status='success'
    ).exists()


def _get_purchased_color(user, product):
    """ رنگی که کاربر هنگام خرید موفق این محصول انتخاب کرده (برای نمایش زیر نظر او) """
    from orders.models import OrderItem
    order_item = OrderItem.objects.filter(
        product=product, order__user=user, order__transactions__status='success', color__isnull=False
    ).order_by('-order__created_at').first()
    return order_item.color if order_item else None


class Review(models.Model):
    """
    نظر یا پاسخ روی یک محصول.
    نظر اصلی (parent=None) امتیاز/عنوان/نقاط‌قوت‌وضعف/عکس دارد؛ پاسخ‌ها (parent ست شده) فقط متن دارند.
    هر دو از چرخه‌ی تایید یکسانی عبور می‌کنند (status پیش‌فرض pending تا تایید ادمین).
    """
    STATUS_CHOICES = (
        ('pending', 'در انتظار تایید'),
        ('published', 'تایید شده'),
        ('rejected', 'رد شده'),
    )

    product = models.ForeignKey(Product, related_name='reviews', on_delete=models.CASCADE, verbose_name='محصول')
    user = models.ForeignKey(CustomUser, related_name='reviews', on_delete=models.CASCADE, verbose_name='کاربر')
    parent = models.ForeignKey('self', null=True, blank=True, related_name='replies', on_delete=models.CASCADE, verbose_name='پاسخ به')
    color = models.ForeignKey(ProductColor, related_name='reviews', on_delete=models.SET_NULL, null=True, blank=True, verbose_name='رنگ خریداری‌شده')

    rating = models.PositiveSmallIntegerField(null=True, blank=True, verbose_name='امتیاز')
    title = models.CharField(max_length=150, blank=True, verbose_name='عنوان')
    body = models.TextField(verbose_name='متن')

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending', verbose_name='وضعیت')
    rejection_reason = models.TextField(blank=True, verbose_name='دلیل رد')
    is_verified_purchase = models.BooleanField(default=False, verbose_name='خریدار تایید شده')

    created_at = models.DateTimeField(auto_now_add=True, verbose_name='تاریخ ثبت')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='آخرین ویرایش')

    class Meta:
        verbose_name = 'نظر'
        verbose_name_plural = 'نظرات'
        ordering = ('-created_at',)
        constraints = [
            models.UniqueConstraint(
                fields=['product', 'user'],
                condition=Q(parent__isnull=True),
                name='one_top_level_review_per_user_product',
            )
        ]

    def __str__(self):
        if self.parent_id:
            return f"پاسخ {self.user} روی نظر #{self.parent_id}"
        return f"نظر {self.user} برای {self.product}"

    def save(self, *args, **kwargs):
        # هر بار که یک نظر اصلی برای اولین‌بار منتشر می‌شود (نه پاسخ)، وضعیت «خریدار تایید‌شده»/رنگ خریداری‌شده
        # را دوباره از روی سفارش‌های واقعی کاربر محاسبه می‌کنیم؛ چون ممکن است بین ثبت نظر و تایید آن، کاربر خرید کرده باشد
        if not self.is_reply and self.status == 'published':
            previous_status = None
            if self.pk:
                previous_status = Review.objects.filter(pk=self.pk).values_list('status', flat=True).first()
            if previous_status != 'published':
                self.is_verified_purchase = _is_verified_purchase(self.user, self.product)
                purchased_color = _get_purchased_color(self.user, self.product)
                if purchased_color:
                    self.color = purchased_color
        super().save(*args, **kwargs)

    @property
    def is_reply(self):
        return self.parent_id is not None

    @property
    def can_edit(self):
        # نظر اصلیِ خودِ کاربر همیشه قابل ویرایش است؛ ویرایش دوباره آن را در صف تایید می‌گذارد
        return not self.is_reply


class ReviewPoint(models.Model):
    """ نقاط قوت/ضعف یک نظر اصلی، هرکدام یک ردیف (برای نمایش آیکون‌دار زیر هم) """
    KIND_CHOICES = (
        ('pro', 'مثبت'),
        ('con', 'منفی'),
    )
    review = models.ForeignKey(Review, related_name='points', on_delete=models.CASCADE, verbose_name='نظر')
    kind = models.CharField(max_length=3, choices=KIND_CHOICES, verbose_name='نوع')
    text = models.CharField(max_length=200, verbose_name='متن')

    class Meta:
        verbose_name = 'نقطه قوت/ضعف'
        verbose_name_plural = 'نقاط قوت و ضعف'

    def __str__(self):
        return f"[{self.get_kind_display()}] {self.text}"


class ReviewImage(models.Model):
    """ تصاویر پیوست‌شده به یک نظر (حداکثر ۳ عدد، در سطح ویو اعمال می‌شود) """
    review = models.ForeignKey(Review, related_name='images', on_delete=models.CASCADE, verbose_name='نظر')
    image = models.ImageField(upload_to='reviews/', verbose_name='تصویر')

    class Meta:
        verbose_name = 'تصویر نظر'
        verbose_name_plural = 'تصاویر نظرات'
