from django.db import models
from accounts.models import CustomUser
from products.models import Product

class Cart(models.Model):
    # از OneToOne استفاده می‌کنیم چون هر کاربر در لحظه فقط یک سبد خریدِ فعال (تسویه‌نشده) دارد
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, related_name='cart', verbose_name='کاربر')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='تاریخ ایجاد')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='آخرین تغییر')

    class Meta:
        verbose_name = 'سبد خرید'
        verbose_name_plural = 'سبدهای خرید'

    def __str__(self):
        return f"سبد خرید {self.user.phone_number}"

    def get_total_price(self):
        """ محاسبه جمع کل مبلغ سبد خرید (با احتساب قیمت‌های ویژه کاربر) """
        return sum(item.get_cost() for item in self.items.all())
        
    def get_total_quantity(self):
        """ محاسبه تعداد کل اقلام موجود در سبد """
        return sum(item.quantity for item in self.items.all())


class CartItem(models.Model):
    cart = models.ForeignKey(Cart, related_name='items', on_delete=models.CASCADE, verbose_name='سبد خرید')
    product = models.ForeignKey(Product, related_name='cart_items', on_delete=models.CASCADE, verbose_name='محصول')
    quantity = models.PositiveIntegerField(default=1, verbose_name='تعداد')

    class Meta:
        verbose_name = 'آیتم سبد خرید'
        verbose_name_plural = 'آیتم‌های سبد خرید'
        # جلوگیری از افزوده شدن یک محصول در دو ردیف مجزا برای یک سبد
        unique_together = ('cart', 'product')

    def __str__(self):
        return f"{self.quantity} {self.product.unit} {self.product.name}"
    def get_cost(self):
        """ 
        محاسبه قیمت این ردیف:
        تعداد × قیمت هوشمند محصول (بر اساس سطح قیمت کاربر صاحبِ این سبد)
        """
        user_price = self.product.get_user_price(self.cart.user)
        return user_price * self.quantity
    