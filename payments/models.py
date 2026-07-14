from django.db import models
from accounts.models import CustomUser
from orders.models import Order

class Transaction(models.Model):
    STATUS_CHOICES = (
        ('pending', 'در انتظار پرداخت'),
        ('success', 'پرداخت موفق'),
        ('failed', 'پرداخت ناموفق'),
    )

    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='transactions', verbose_name='کاربر')
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='transactions', verbose_name='سفارش')
    
    amount = models.DecimalField(max_digits=12, decimal_places=0, verbose_name='مبلغ (تومان)')
    authority = models.CharField(max_length=100, unique=True, verbose_name='کد ارجاع درگاه (Authority)')
    ref_id = models.CharField(max_length=100, blank=True, null=True, verbose_name='شماره پیگیری بانک (RefID)')
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name='وضعیت')
    
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='تاریخ ایجاد')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='آخرین تغییر')

    class Meta:
        verbose_name = 'تراکنش'
        verbose_name_plural = 'تراکنش‌ها'

    def __str__(self):
        return f"{self.authority} - {self.status}"
    