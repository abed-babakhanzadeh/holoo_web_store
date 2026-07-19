from django.db import models
from accounts.models import CustomUser
from products.models import Product, ProductColor

class Order(models.Model):
    # --- وضعیت‌های سفارش ---
    STATUS_CHOICES = (
        ('pending', 'در انتظار پرداخت / بررسی'),
        ('registered', 'ثبت شده در حسابداری'), # <--- این وضعیت اضافه شد
        ('processing', 'در حال آماده‌سازی انبار'),
        ('shipped', 'ارسال شده'),
        ('delivered', 'تحویل داده شده'),
        ('canceled', 'لغو شده'),
    )

    # --- وضعیت نمایشی به کاربر (متفاوت از وضعیت داخلی حسابداری) ---
    # چون ثبت فاکتور در هلو (status='registered') مستقل از موفقیت پرداخت انجام می‌شود،
    # نمی‌توان صرفاً بر اساس status تشخیص داد که سفارش «در انتظار پرداخت» است یا نه.
    CUSTOMER_STATUS_CHOICES = (
        ('awaiting_payment', 'در انتظار پرداخت / بررسی'),
        ('processing', 'در حال آماده‌سازی انبار'),
        ('shipped', 'ارسال شده'),
        ('delivered', 'تحویل داده شده'),
        ('canceled', 'لغو شده'),
    )

    # --- روش‌های پرداخت (متصل به قیمت‌های هلو) ---
    PAYMENT_METHODS = (
        ('cash', 'نقدی (قیمت 1)'),
        ('check', 'چکی (قیمت 2)'),
        ('installment', 'اقساطی (قیمت 3)'),
    )

    user = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, related_name='orders', verbose_name='کاربر')
    
    # --- اطلاعات گیرنده (در لحظه ثبت سفارش کپی می‌شود تا اگر کاربر بعدا آدرسش را عوض کرد، فاکتور قدیمی خراب نشود) ---
    first_name = models.CharField(max_length=50, verbose_name='نام گیرنده')
    last_name = models.CharField(max_length=50, verbose_name='نام خانوادگی گیرنده')
    phone = models.CharField(max_length=15, verbose_name='شماره تماس گیرنده')
    address = models.TextField(verbose_name='آدرس کامل')
    postal_code = models.CharField(max_length=20, blank=True, null=True, verbose_name='کد پستی')

    # --- اطلاعات مالی فاکتور ---
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHODS, default='cash', verbose_name='روش پرداخت')
    shipping_cost = models.DecimalField(max_digits=12, decimal_places=0, default=0, verbose_name='هزینه ارسال')
    total_price = models.DecimalField(max_digits=12, decimal_places=0, verbose_name='مبلغ کل سفارش')
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name='وضعیت سفارش')

    # --- ارتباط با حسابداری هلو ---
    holoo_invoice_id = models.CharField(max_length=50, blank=True, null=True, verbose_name='شماره فاکتور در هلو')

    created_at = models.DateTimeField(auto_now_add=True, verbose_name='تاریخ ثبت')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='آخرین بروزرسانی')

    class Meta:
        verbose_name = 'سفارش'
        verbose_name_plural = 'سفارشات'
        ordering = ('-created_at',)

    def __str__(self):
        return f"سفارش #{self.id} - {self.user.phone_number}"

    @property
    def is_paid(self):
        """ آیا این سفارش تراکنش پرداخت موفق دارد """
        return self.transactions.filter(status='success').exists()

    @property
    def can_pay(self):
        """
        آیا امکان شروع/تلاش مجدد پرداخت آنلاین برای این سفارش وجود دارد.
        وضعیت سفارش با کمی تاخیر (پس از تایید هلو) به‌روز می‌شود، پس صرفاً برای
        جلوگیری از پرداخت دوباره در همین فاصله، عدم وجود تراکنش موفق را هم چک می‌کنیم.
        """
        return self.status in ('pending', 'registered') and not self.is_paid

    @property
    def customer_status(self):
        """ وضعیت واقعی از دید مشتری، بدون توجه به مراحل داخلی حسابداری هلو """
        if self.status == 'canceled':
            return 'canceled'
        if not self.is_paid:
            return 'awaiting_payment'
        if self.status in ('pending', 'registered'):
            # پرداخت با موفقیت انجام شده اما سند دریافت وجه هنوز در هلو تایید نشده (تسک پس‌زمینه)
            return 'processing'
        return self.status

    @property
    def customer_status_display(self):
        return dict(self.CUSTOMER_STATUS_CHOICES).get(self.customer_status, self.get_status_display())


class OrderItem(models.Model):
    order = models.ForeignKey(Order, related_name='items', on_delete=models.CASCADE, verbose_name='سفارش')
    product = models.ForeignKey(Product, related_name='order_items', on_delete=models.SET_NULL, null=True, verbose_name='محصول')
    color = models.ForeignKey(ProductColor, related_name='order_items', on_delete=models.SET_NULL, null=True, blank=True, verbose_name='رنگ خریداری‌شده')

    # اینجا قیمت را ذخیره می‌کنیم تا اگر فردا قیمت کالا در هلو عوض شد، فاکتورهای قدیمی سایت تغییر نکنند (Freeze)
    price = models.DecimalField(max_digits=12, decimal_places=0, verbose_name='قیمت ثبت شده')
    quantity = models.PositiveIntegerField(default=1, verbose_name='تعداد')

    class Meta:
        verbose_name = 'آیتم سفارش'
        verbose_name_plural = 'آیتم‌های سفارش'

    def __str__(self):
        return f"{self.quantity} {self.product.unit} {self.product.name}"

    def get_cost(self):
        return self.price * self.quantity
    