from django.db import models
from accounts.models import CustomUser
from products.models import Product


class FavoriteProduct(models.Model):
    """ محصولات علاقه‌مندی هر کاربر """
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='favorite_products', verbose_name='کاربر')
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='favorited_by', verbose_name='محصول')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='تاریخ افزودن')

    class Meta:
        verbose_name = 'محصول علاقه‌مندی'
        verbose_name_plural = 'محصولات علاقه‌مندی'
        unique_together = ('user', 'product')
        ordering = ('-created_at',)

    def __str__(self):
        return f"{self.user.phone_number} - {self.product.name}"
