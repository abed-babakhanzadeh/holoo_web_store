"""
منطق مشترک افزودن/کاهش آیتم سبد خرید — تنها محل نوشتن روی CartItem.

قبل از این ماژول، همین منطق در سه جای جدا تکرار شده بود (cart/views.py دو بار و
orders/views.py::CheckoutCartUpdateView) و هر سه یک race یکسان داشتند:

  - ابتدا filter().first() و بعد create(): بین خواندن و نوشتن، دو ریکوئست هم‌زمان
    (مثلاً دابل‌کلیک کاربر) هر دو ردیف را «وجود ندارد» می‌دیدند و هر دو create می‌زدند.
  - quantity += 1 در پایتون بدون F(): دو ریکوئست هم‌زمان همان مقدار قدیمی quantity را
    می‌خواندند و یکی از دو افزایش گم می‌شد (lost update).

راه‌حل اینجا قفل‌کردن خودِ ردیف Cart (نه ردیف CartItem) قبل از هر خواندن/نوشتنی روی
آیتم‌های آن است. این عمداً درشت‌دانه‌تر از قفل تک‌ردیفی است، اما لازم است:

  با یک تست همزمانی واقعی (cart/tests.py::CartRaceConditionTests) ثابت شد که تکیه بر
  select_for_update روی خودِ ردیف CartItem (و مدیریت IntegrityError برای مسیر create)
  برای محصولات *بدون رنگ* کافی نیست. دلیلش رفتار خاص بک‌اند mssql-django است: برای
  هماهنگ‌کردن معنای NULL با بقیه‌ی بک‌اندهای جنگو، ایندکس یکتای (cart, product, color)
  را به‌صورت فیلترشده با شرط «هر سه ستون NOT NULL» می‌سازد؛ یعنی وقتی color خالی است
  (اکثریت محصولات)، این ایندکس هیچ محافظتی نمی‌دهد و چند ردیف با cart+product+NULL
  یکسان بدون هیچ خطایی ساخته می‌شوند. چون ردیف هنوز وجود ندارد، select_for_update روی
  آن هم چیزی برای قفل‌کردن پیدا نمی‌کند. قفل‌کردن سبد که همیشه از قبل وجود دارد، این
  حفره را از ریشه می‌بندد: دو ریکوئست هم‌زمان برای یک کاربر واحد، پشت سر هم صف می‌شوند.
"""
from django.db import transaction
from django.db.models import F

from .models import Cart, CartItem


def _locked_cart(cart):
    return Cart.objects.select_for_update().get(pk=cart.pk)


def add_item(cart, product, color_id=None):
    """
    یک واحد از (محصول + رنگ انتخابی) را به سبد اضافه می‌کند.

    اگر ردیفی برای این ترکیب وجود نداشت و موجودی محصول صفر (یا کمتر) بود، کاری انجام
    نمی‌شود. اگر تعداد فعلی ردیف به سقف موجودی رسیده باشد، بدون خطا همان ردیف بدون
    تغییر برمی‌گردد (سقف در سمت کاربر با دیسیبل‌شدن دکمه‌ی + هم رعایت می‌شود؛ این یک
    محافظت سمت سرور در برابر ریکوئست دستکاری‌شده است).

    خروجی: نمونه‌ی به‌روزشده‌ی CartItem، یا None اگر ردیفی برای برگرداندن وجود نداشت.
    """
    with transaction.atomic():
        _locked_cart(cart)
        cart_item = CartItem.objects.filter(cart=cart, product=product, color_id=color_id).first()

        if cart_item is None:
            if product.stock <= 0:
                return None
            return CartItem.objects.create(cart=cart, product=product, color_id=color_id, quantity=1)

        if cart_item.quantity < product.stock:
            CartItem.objects.filter(pk=cart_item.pk).update(quantity=F('quantity') + 1)
            cart_item.refresh_from_db(fields=['quantity'])
        return cart_item


def decrease_item(cart, product, color_id=None):
    """
    یک واحد از تعداد ردیف کم می‌کند؛ اگر تعداد به صفر برسد، کل ردیف حذف می‌شود.

    خروجی: نمونه‌ی به‌روزشده‌ی CartItem، یا None اگر ردیف حذف شد یا از اول وجود نداشت.
    """
    with transaction.atomic():
        _locked_cart(cart)
        cart_item = CartItem.objects.filter(cart=cart, product=product, color_id=color_id).first()
        if cart_item is None:
            return None

        if cart_item.quantity > 1:
            CartItem.objects.filter(pk=cart_item.pk).update(quantity=F('quantity') - 1)
            cart_item.refresh_from_db(fields=['quantity'])
            return cart_item

        cart_item.delete()
        return None
