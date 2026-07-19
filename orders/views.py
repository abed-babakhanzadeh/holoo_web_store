from datetime import timedelta
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views import View
from django.views.generic import TemplateView
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.db import transaction
from django.http import HttpResponse
from products.models import Product
from cart.models import CartItem
from cart.models import Cart
from .models import Order, OrderItem
from holoo.tasks import send_order_to_holoo

# هزینه ثابت ارسال (در پروژه‌های بزرگ می‌تواند بر اساس شهر داینامیک باشد)
SHIPPING_COST = 200000

def get_price_by_method(product, method):
    """ استخراج هوشمند قیمت بر اساس روش پرداخت """
    if method == 'check':
        return product.price2
    elif method == 'installment':
        return product.price3
    return product.price # پیش‌فرض نقدی (price1)


class CheckoutView(LoginRequiredMixin, TemplateView):
    """ نمایش صفحه تسویه حساب """
    template_name = 'orders/checkout.html'

    def get(self, request, *args, **kwargs):
        cart = Cart.objects.filter(user=request.user).first()
        # اگر سبد خریدی وجود نداشت یا خالی بود، برگرد به صفحه محصولات
        if not cart or cart.items.count() == 0:
            return redirect('products:product_list')
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['cart'] = Cart.objects.filter(user=self.request.user).first()
        context['shipping_cost'] = SHIPPING_COST
        return context


class UpdateInvoiceView(LoginRequiredMixin, TemplateView):
    """ ویوی مخصوص HTMX برای محاسبه لایو فاکتور هنگام تغییر روش پرداخت """
    template_name = 'orders/partials/invoice.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        method = self.request.GET.get('payment_method', 'cash')
        cart = get_object_or_404(Cart, user=self.request.user)
        
        total_items_price = sum(
            get_price_by_method(item.product, method) * item.quantity 
            for item in cart.items.all()
        )
        final_total = total_items_price + SHIPPING_COST

        context.update({
            'total_items_price': total_items_price,
            'shipping_cost': SHIPPING_COST,
            'final_total': final_total,
            'method': method,
        })
        return context


class SubmitOrderView(LoginRequiredMixin, View):
    """ ثبت نهایی، قفل کردن قیمت‌ها، پاک کردن سبد و ارسال به هلو """
    
    # تضمین می‌کند که اگر وسط کار خطایی رخ داد، دیتابیس خراب نشود
    @method_decorator(transaction.atomic)
    def post(self, request, *args, **kwargs):
        cart = get_object_or_404(Cart, user=request.user)
        method = request.POST.get('payment_method', 'cash')
        
        # ۱. محاسبه قیمت نهایی برای ذخیره در فاکتور
        total_items_price = sum(get_price_by_method(item.product, method) * item.quantity for item in cart.items.all())
        final_total = total_items_price + SHIPPING_COST

        # ۲. ساخت سفارش جدید
        order = Order.objects.create(
            user=request.user,
            first_name=request.POST.get('first_name', request.user.first_name),
            last_name=request.POST.get('last_name', request.user.last_name),
            phone=request.POST.get('phone', request.user.phone_number),
            address=request.POST.get('address', request.user.address),
            postal_code=request.POST.get('postal_code', ''),
            payment_method=method,
            shipping_cost=SHIPPING_COST,
            total_price=final_total,
        )

        # ۳. کپی کردن آیتم‌ها و قفل کردن قیمتِ همان لحظه
        for item in cart.items.all():
            OrderItem.objects.create(
                order=order,
                product=item.product,
                color=item.color,
                price=get_price_by_method(item.product, method),
                quantity=item.quantity
            )

        # ۴. پاک کردن سبد خرید
        cart.delete()

        # ۵. شلیک تسک به سمت هلو (در بک‌گراند اجرا می‌شود تا کاربر منتظر نماند)
        send_order_to_holoo.delay(order.id)

        # ۶. هدایت به صفحه موفقیت
        return redirect('orders:order_success', order_id=order.id)


class OrderSuccessView(LoginRequiredMixin, TemplateView):
    """ نمایش صفحه موفقیت سفارش """
    template_name = 'orders/success.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # order_id از طریق URL به این متد پاس داده می‌شود
        context['order'] = get_object_or_404(Order, id=self.kwargs['order_id'], user=self.request.user)
        return context
    
class CheckoutCartUpdateView(LoginRequiredMixin, View):
    """ آپدیت تعداد کالاهای سبد مستقیماً از داخل صفحه تسویه حساب """
    
    def post(self, request, product_id, action, *args, **kwargs):
        product = get_object_or_404(Product, id=product_id)
        cart = get_object_or_404(Cart, user=request.user)

        if action == 'add':
            cart_item, created = CartItem.objects.get_or_create(cart=cart, product=product)
            if not created and cart_item.quantity < product.stock:
                cart_item.quantity += 1
                cart_item.save()
        
        elif action == 'decrease':
            try:
                cart_item = CartItem.objects.get(cart=cart, product=product)
                if cart_item.quantity > 1:
                    cart_item.quantity -= 1
                    cart_item.save()
                else:
                    cart_item.delete()
            except CartItem.DoesNotExist:
                pass

        # اگر کاربر همه کالاها را حذف کرد، او را به فروشگاه برگردان
        if cart.items.count() == 0:
            response = HttpResponse()
            response['HX-Redirect'] = '/' # انتقال کل صفحه با HTMX
            return response

        # رندر کردن مجدد لیست اقلام سبد خرید
        response = render(request, 'orders/partials/checkout_cart_items.html', {'cart': cart})
        # این سیگنال باعث می‌شود مینی‌کارت و باکس فاکتور خودشان را آپدیت کنند!
        response['HX-Trigger'] = 'cartUpdated'
        return response
    
class UserOrderHistoryView(LoginRequiredMixin, TemplateView):
    """ نمایش سوابق سفارشات کاربر در پنل کاربری، با امکان فیلتر واقعی روی وضعیت/بازه‌ی زمانی/مبلغ """
    template_name = 'orders/history.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        orders = Order.objects.filter(user=self.request.user).order_by('-created_at')

        status = self.request.GET.get('status', '')
        date_range = self.request.GET.get('date_range', '')
        amount_range = self.request.GET.get('amount_range', '')

        if status == 'awaiting_payment':
            # هم سفارش‌های تازه ثبت‌شده (pending) و هم آن‌هایی که فاکتورشان در هلو ثبت شده
            # (registered) اما هنوز پرداخت موفق ندارند، در این دسته قرار می‌گیرند.
            orders = orders.filter(status__in=['pending', 'registered']).exclude(transactions__status='success')
        elif status:
            orders = orders.filter(status=status)

        if date_range:
            days_map = {'7days': 7, '30days': 30, '3months': 90, 'year': 365}
            days = days_map.get(date_range)
            if days:
                orders = orders.filter(created_at__gte=timezone.now() - timedelta(days=days))

        if amount_range:
            bounds_map = {
                'less500': (None, 500000),
                '500-1000': (500000, 1000000),
                '1000-5000': (1000000, 5000000),
                'more5000': (5000000, None),
            }
            bounds = bounds_map.get(amount_range)
            if bounds:
                low, high = bounds
                if low is not None:
                    orders = orders.filter(total_price__gte=low)
                if high is not None:
                    orders = orders.filter(total_price__lt=high)

        context['active_nav'] = 'orders'
        context['orders'] = orders
        context['selected_status'] = status
        context['selected_date_range'] = date_range
        context['selected_amount_range'] = amount_range
        context['status_choices'] = Order.CUSTOMER_STATUS_CHOICES
        return context
    

# ترتیب واقعی مراحل یک سفارش (برای نوار پیشرفت جزئیات سفارش)
ORDER_STATUS_STEPS = [
    ('pending', 'ثبت سفارش'),
    ('registered', 'تایید و ثبت در حسابداری'),
    ('processing', 'آماده‌سازی در انبار'),
    ('shipped', 'ارسال شده'),
    ('delivered', 'تحویل شده'),
]


def build_status_steps(order):
    """ محاسبه‌ی نوار پیشرفت وضعیت سفارش؛ (is_canceled, status_steps) را برمی‌گرداند """
    if order.status == 'canceled':
        return True, []

    status_keys = [key for key, _ in ORDER_STATUS_STEPS]
    current_index = status_keys.index(order.status) if order.status in status_keys else 0
    status_steps = [
        {'label': label, 'done': i <= current_index}
        for i, (key, label) in enumerate(ORDER_STATUS_STEPS)
    ]
    return False, status_steps


class OrderDetailView(LoginRequiredMixin, TemplateView):
    """ نمایش جزئیات یک سفارش با HTMX در داشبورد """
    template_name = 'orders/partials/order_detail.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # فقط سفارشی که متعلق به همین کاربر است را می‌آوریم (امنیت)
        order = get_object_or_404(Order, id=self.kwargs['order_id'], user=self.request.user)
        context['order'] = order
        context['is_canceled'], context['status_steps'] = build_status_steps(order)
        return context


class OrderFullDetailView(LoginRequiredMixin, TemplateView):
    """ صفحه‌ی کامل جزئیات یک سفارش (تصویر محصولات، خلاصه سفارش، اکشن‌ها) """
    template_name = 'orders/order_full_detail.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        order = get_object_or_404(Order, id=self.kwargs['order_id'], user=self.request.user)
        context['order'] = order
        context['active_nav'] = 'orders'
        context['is_canceled'], context['status_steps'] = build_status_steps(order)
        context['items_subtotal'] = sum(item.get_cost() for item in order.items.all())
        context['paid_transaction'] = order.transactions.filter(status='success').order_by('-created_at').first()
        return context
    