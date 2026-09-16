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
# قیمت‌گذاری (روش پرداخت + سطح قیمت + تخفیف فعال) تماماً در products/pricing.py متمرکز شده
# تا فاکتور، سبد خرید و کارت محصول هرگز سه عدد متفاوت نشان ندهند.
from products.pricing import default_payment_method, final_price, resolve_payment_method
from cart.models import CartItem
from cart.models import Cart
from .forms import CheckoutForm
from .models import Order, OrderItem
from .signals import order_placed

# هزینه ثابت ارسال (در پروژه‌های بزرگ می‌تواند بر اساس شهر داینامیک باشد)
SHIPPING_COST = 200000


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
        # روش پرداختی که فرم در اولین رندر تیک می‌زند؛ ردیف‌های سبد باید با همین قیمت‌گذاری
        # شوند تا با باکس فاکتور (UpdateInvoiceView) اختلاف نداشته باشند
        context['method'] = default_payment_method(self.request.user)
        return context


class UpdateInvoiceView(LoginRequiredMixin, TemplateView):
    """ ویوی مخصوص HTMX برای محاسبه لایو فاکتور هنگام تغییر روش پرداخت """
    template_name = 'orders/partials/invoice.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        method = resolve_payment_method(self.request.user, self.request.GET.get('payment_method', 'cash'))
        cart = get_object_or_404(Cart, user=self.request.user)

        total_items_price = sum(
            final_price(item.product, self.request.user, method) * item.quantity
            for item in cart.items.select_related('product')
        )
        final_total = total_items_price + SHIPPING_COST

        context.update({
            'total_items_price': total_items_price,
            'shipping_cost': SHIPPING_COST,
            'final_total': final_total,
            'method': method,
            # ردیف‌های سبد هم با همین پاسخ (به‌صورت OOB) دوباره رندر می‌شوند تا با تغییر روش
            # پرداخت، فیِ هر ردیف همان لحظه با جمع فاکتور هماهنگ شود
            'cart': cart,
        })
        return context


class SubmitOrderView(LoginRequiredMixin, View):
    """ ثبت نهایی، قفل کردن قیمت‌ها، پاک کردن سبد و اعلام رویداد ثبت سفارش """
    template_name = 'orders/checkout.html'

    # تضمین می‌کند که اگر وسط کار خطایی رخ داد، دیتابیس خراب نشود
    @method_decorator(transaction.atomic)
    def post(self, request, *args, **kwargs):
        cart = get_object_or_404(Cart, user=request.user)

        # ۰. اعتبارسنجی اطلاعات گیرنده. قبلاً هیچ اعتبارسنجی‌ای نبود و سفارش با آدرس/نام
        # خالی یا شماره‌ی نامعتبر هم ثبت می‌شد و همان داده به فاکتور هلو می‌رفت.
        form = CheckoutForm(request.POST, user=request.user)
        if not form.is_valid():
            return render(request, self.template_name, {
                'cart': cart,
                'shipping_cost': SHIPPING_COST,
                'method': default_payment_method(request.user),
                'error': form.error_text,
            })

        method = resolve_payment_method(request.user, form.cleaned_data['payment_method'])

        # ۱. قیمت هر ردیف دقیقاً یک بار محاسبه می‌شود و همان مقدار هم در جمع فاکتور و هم در
        # OrderItem.price می‌نشیند؛ قبلاً دو بار جدا محاسبه می‌شد و اگر تخفیف محصول دقیقاً بین
        # این دو محاسبه منقضی می‌شد، جمع فاکتور با مجموع ردیف‌هایش نمی‌خواند.
        priced_items = [
            (item, final_price(item.product, request.user, method))
            for item in cart.items.select_related('product').prefetch_related('product__discounts')
        ]
        total_items_price = sum(price * item.quantity for item, price in priced_items)
        final_total = total_items_price + SHIPPING_COST

        # ۲. ساخت سفارش جدید
        order = Order.objects.create(
            user=request.user,
            first_name=form.cleaned_data['first_name'],
            last_name=form.cleaned_data['last_name'],
            phone=form.cleaned_data['phone'],
            address=form.cleaned_data['address'],
            postal_code=form.cleaned_data['postal_code'],
            payment_method=method,
            shipping_cost=SHIPPING_COST,
            total_price=final_total,
        )

        # ۳. کپی کردن آیتم‌ها و قفل کردن قیمتِ همان لحظه (همان قیمتی که بالا جمع زده شد)
        OrderItem.objects.bulk_create([
            OrderItem(
                order=order,
                product=item.product,
                color=item.color,
                price=price,
                quantity=item.quantity,
            )
            for item, price in priced_items
        ])

        # ۴. پاک کردن سبد خرید
        cart.delete()

        # ۵. اعلام رویداد «سفارش ثبت شد».
        # این اپ نمی‌داند و لازم نیست بداند چه کسی به این رویداد گوش می‌دهد (ثبت فاکتور در
        # حسابداری، اطلاع‌رسانی، هر چیز دیگر). با on_commit تا زمانی که تراکنش واقعاً commit
        # نشده رویداد منتشر نمی‌شود؛ وگرنه شنونده‌ای که سفارش را از دیتابیس می‌خواند با
        # DoesNotExist مواجه می‌شود. send_robust تا خطای یک شنونده مسیر کاربر را نشکند.
        transaction.on_commit(lambda: order_placed.send_robust(sender=Order, order=order))

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
        color_id = request.POST.get('color_id') or None

        if action == 'add':
            cart_item = CartItem.objects.filter(cart=cart, product=product, color_id=color_id).first()
            if cart_item is None:
                # ردیف جدید فقط وقتی ساخته شود که واقعاً موجودی داشته باشیم
                if product.stock > 0:
                    CartItem.objects.create(cart=cart, product=product, color_id=color_id, quantity=1)
            elif cart_item.quantity < product.stock:
                cart_item.quantity += 1
                cart_item.save()

        elif action == 'decrease':
            try:
                cart_item = CartItem.objects.get(cart=cart, product=product, color_id=color_id)
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

        # رندر کردن مجدد لیست اقلام سبد خرید، با همان روش پرداختی که همین الان در فرم تیک خورده
        # (فرم آن را با hx-include می‌فرستد) تا قیمت ردیف‌ها با باکس فاکتور یکی بماند
        method = resolve_payment_method(request.user, request.POST.get('payment_method'))
        response = render(request, 'orders/partials/checkout_cart_items.html', {'cart': cart, 'method': method})
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
    