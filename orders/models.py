from decimal import Decimal

from django.db import models
from accounts.models import CustomUser
from products.models import Product, ProductColor
from products.pricing import PAYMENT_METHODS as PRICING_PAYMENT_METHODS

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
    # چکی = price1 ، نقدی = price2 ، ویژه (VIP) = price3 (نگاشت واقعی سطوح قیمت هلو طبق کارفرما)
    # تعریف واحد در products/pricing.py است تا نگاشت «روش پرداخت -> ستون قیمت» فقط یک جا بماند
    PAYMENT_METHODS = PRICING_PAYMENT_METHODS

    user = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, related_name='orders', verbose_name='کاربر')
    
    # --- اطلاعات گیرنده (در لحظه ثبت سفارش کپی می‌شود تا اگر کاربر بعدا آدرسش را عوض کرد، فاکتور قدیمی خراب نشود) ---
    first_name = models.CharField(max_length=50, verbose_name='نام گیرنده')
    last_name = models.CharField(max_length=50, verbose_name='نام خانوادگی گیرنده')
    phone = models.CharField(max_length=15, verbose_name='شماره تماس گیرنده')
    address = models.TextField(verbose_name='آدرس کامل')
    postal_code = models.CharField(max_length=20, blank=True, null=True, verbose_name='کد پستی')

    # --- اسنپ‌شات مقصد و روش ارسال (متن/عدد کپی‌شده در لحظه‌ی ثبت؛ عمداً ForeignKey نیست) ---
    # با تغییر یا حذف آدرس کاربر، تغییر نام شهر/ناحیه یا عوض شدن تعرفه‌ی پیک، فاکتورهای قبلی دست نمی‌خورند.
    # سفارش‌های قدیمی (پیش از این فیلدها) خالی می‌مانند و address همان متن کامل قدیمی است.
    # «address» برای سفارش‌های جدید فقط بخش خیابان/پلاک است؛ متن کامل را full_address می‌سازد.
    province = models.CharField(max_length=100, blank=True, default='', verbose_name='استان')
    city = models.CharField(max_length=100, blank=True, default='', verbose_name='شهر')
    zone = models.CharField(max_length=100, blank=True, default='', verbose_name='ناحیه')

    SHIPPING_METHOD_CHOICES = (
        ('courier', 'ارسال با پیک'),
        ('post', 'ارسال با پست (پس‌کرایه)'),
    )
    shipping_method = models.CharField(max_length=10, choices=SHIPPING_METHOD_CHOICES, blank=True, default='', verbose_name='روش ارسال')
    # متنِ نمایش‌داده‌شده‌ی ارسال در لحظه‌ی ثبت (مثلاً «ارسال با پیک» یا «پس‌کرایه (پرداخت هزینه درب منزل)»)
    shipping_label = models.CharField(max_length=200, blank=True, default='', verbose_name='برچسب ارسال')

    # --- اطلاعات مالی فاکتور ---
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHODS, default='cash', verbose_name='روش پرداخت')
    shipping_cost = models.DecimalField(max_digits=12, decimal_places=0, default=0, verbose_name='هزینه ارسال')
    total_price = models.DecimalField(max_digits=12, decimal_places=0, verbose_name='مبلغ کل سفارش')

    # --- اسنپ‌شات تخفیف (عدد/متن کپی‌شده در لحظه‌ی ثبت؛ عمداً ForeignKey به Promotion/کوپن نیست) ---
    # با ویرایش/حذف/پایان کمپین‌ها، فاکتورهای گذشته دست نمی‌خورند. سفارش‌های قدیمی (پیش از این فیلدها) صفر می‌مانند
    # (صفر یعنی «ثبت نشده»، نه «تخفیفی نبود»؛ چون تخفیف قدیمی در قیمتِ ردیف‌ها نشسته و قیمت اصلی‌اش معلوم نیست).
    #   قیمت ردیف‌ها (OrderItem.price) همیشه *بعد از* تخفیف‌های خودکار است؛ total_price =
    #   Σ(price×qty) − order_discount + shipping_cost
    # promotion_discount: جمع تخفیف‌های خودکار همه‌ی ردیف‌ها = Σ(OrderItem.discount_amount × quantity)
    promotion_discount = models.DecimalField(max_digits=12, decimal_places=0, default=0, verbose_name='جمع تخفیف‌های خودکار')
    # order_discount: تخفیفِ سطح سفارش (کد تخفیف، مرحله‌ی ۳)؛ روی «مبلغ کالا» و بعد از تخفیف‌های خودکار. در هلو متناسب
    # روی فی اقلام پخش می‌شود (holoo/invoice.py)
    order_discount = models.DecimalField(max_digits=12, decimal_places=0, default=0, verbose_name='تخفیف سطح سفارش (کد تخفیف)')
    order_discount_label = models.CharField(max_length=200, blank=True, default='', verbose_name='عنوان تخفیف سطح سفارش')
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', verbose_name='وضعیت سفارش')
    # وقتی ادمین این را همراه با status='shipped' پر/ثبت کند، پیامک کد رهگیری برای مشتری
    # می‌رود (نگاه کنید OrderAdmin.save_model)
    tracking_code = models.CharField(max_length=50, blank=True, null=True, verbose_name='کد رهگیری پستی')

    # --- ارتباط با حسابداری هلو ---
    holoo_invoice_id = models.CharField(max_length=50, blank=True, null=True, verbose_name='شماره فاکتور در هلو')
    # شماره سند دریافت وجه در هلو. پر بودن این فیلد یعنی «وجه این سفارش قبلاً در حسابداری ثبت
    # شده»؛ همین تضمین می‌کند که رفرش صفحه‌ی بازگشت از درگاه یا تلاش مجدد تسک، سند تکراری نسازد.
    holoo_receipt_id = models.CharField(max_length=50, blank=True, null=True, verbose_name='شماره سند دریافت وجه در هلو')
    # وقتی ثبت فاکتور در هلو بیش از چند روز طول بکشد (مثلاً قطعی طولانی شبکه/هلو)، این
    # پرچم یک‌بار True می‌شود تا سفارش گیرکرده در پنل ادمین قابل پیدا کردن باشد (تلاش خودکار
    # پس‌زمینه همچنان ادامه دارد، این فقط برای اطلاع/پیگیری دستی است)
    holoo_sync_alert_sent = models.BooleanField(default=False, verbose_name='هشدار تاخیر ثبت در هلو ارسال شد')

    created_at = models.DateTimeField(auto_now_add=True, verbose_name='تاریخ ثبت')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='آخرین بروزرسانی')

    class Meta:
        verbose_name = 'سفارش'
        verbose_name_plural = 'سفارشات'
        ordering = ('-created_at',)
        constraints = [
            models.CheckConstraint(condition=models.Q(promotion_discount__gte=0), name='order_promotion_discount_gte_0'),
            models.CheckConstraint(condition=models.Q(order_discount__gte=0), name='order_order_discount_gte_0'),
        ]

    def __str__(self):
        return f"سفارش #{self.id} - {self.user.phone_number}"

    @property
    def items_total(self):
        """ «مبلغ کالاها» پس از تخفیف‌های خودکار (جمع ردیف‌ها با قیمت ثبت‌شده) """
        return sum((item.get_cost() for item in self.items.all()), Decimal('0'))

    @property
    def items_original_total(self):
        """ جمع ردیف‌ها با قیمت پایه‌ی قبل از تخفیف (سفارش قدیمی: همان مبلغ کالاها) """
        return sum((item.original_cost for item in self.items.all()), Decimal('0'))

    @property
    def has_discount(self):
        return self.promotion_discount > 0 or self.order_discount > 0

    @property
    def total_discount(self):
        return self.promotion_discount + self.order_discount

    @property
    def computed_total(self):
        """ مبلغ قابل پرداخت از روی ردیف‌ها: کالاها − تخفیف سطح سفارش + ارسال (برای سنجش سازگاری total_price) """
        return self.items_total - self.order_discount + self.shipping_cost

    @property
    def shipping_title(self):
        """ عنوان روش ارسال برای نمایش («ارسال با پیک»، «ارسال با پست (پس‌کرایه)»)؛ سفارش قدیمی: «هزینه ارسال» """
        return dict(self.SHIPPING_METHOD_CHOICES).get(self.shipping_method) or 'هزینه ارسال'

    @property
    def full_address(self):
        """ «استان، شهر، ناحیه، آدرس»؛ برای سفارش‌های قدیمی (بدون استان/شهر) همان متن آدرس ذخیره‌شده """
        parts = [self.province, self.city, self.zone, self.address]
        return '، '.join(p.strip() for p in parts if p and p.strip())

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
    # اسنپ‌شات تخفیف *هر واحد*: original_price = قیمت پایه‌ی قبل از تخفیف‌های خودکار (سطح قیمت + روش پرداخت)،
    # discount_amount = مقدار کم‌شده؛ همیشه price = original_price − discount_amount. سفارش قدیمی: هر دو صفر (ثبت نشده).
    original_price = models.DecimalField(max_digits=12, decimal_places=0, default=0, verbose_name='قیمت اصلی (قبل از تخفیف)')
    discount_amount = models.DecimalField(max_digits=12, decimal_places=0, default=0, verbose_name='تخفیف هر واحد')
    quantity = models.PositiveIntegerField(default=1, verbose_name='تعداد')

    class Meta:
        verbose_name = 'آیتم سفارش'
        verbose_name_plural = 'آیتم‌های سفارش'
        constraints = [
            models.CheckConstraint(condition=models.Q(discount_amount__gte=0), name='orderitem_discount_amount_gte_0'),
            # یا اسنپ‌شات ندارد (قدیمی/صفر) یا قیمت اصلی دقیقاً برابر قیمتِ ثبت‌شده + تخفیف است
            models.CheckConstraint(
                condition=models.Q(original_price=0) | models.Q(original_price=models.F('price') + models.F('discount_amount')),
                name='orderitem_original_eq_price_plus_discount',
            ),
        ]

    def __str__(self):
        return f"{self.quantity} {self.product.unit} {self.product.name}"

    def get_cost(self):
        return self.price * self.quantity

    @property
    def unit_original_price(self):
        """ قیمت اصلی هر واحد؛ برای سفارش قدیمی (بدون اسنپ‌شات) همان قیمت ثبت‌شده """
        return self.original_price or self.price

    @property
    def original_cost(self):
        return self.unit_original_price * self.quantity

    @property
    def line_discount(self):
        """ تخفیف کل ردیف (تعداد × تخفیف هر واحد) """
        return self.discount_amount * self.quantity

    @property
    def has_discount(self):
        return self.discount_amount > 0

    @property
    def discount_percent(self):
        base = self.unit_original_price
        if not self.has_discount or base <= 0:
            return 0
        return int(round(self.discount_amount * 100 / base))
    