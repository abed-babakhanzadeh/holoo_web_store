from datetime import timedelta
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views import View
from django.views.generic import TemplateView
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.db import transaction
from django.http import HttpResponse
from products.models import Product, SiteSettings
# قیمت‌گذاری (روش پرداخت + سطح قیمت + تخفیف فعال) تماماً در products/pricing.py متمرکز شده
# تا فاکتور، سبد خرید و کارت محصول هرگز سه عدد متفاوت نشان ندهند.
from products.pricing import default_payment_method, final_price, resolve_payment_method
from cart.models import Cart
from cart.services import add_item, decrease_item
from .checkout import address_options, get_user_address
from .forms import CheckoutForm
from .models import Order, OrderItem
from .shipping import shipping_quote
from .signals import order_placed
from .snapshot import order_snapshot


def build_checkout_context(request, cart, selected_address=None, error=None):
    """
    زمینه‌ی صفحه‌ی تسویه‌حساب (هم برای نمایش اول و هم برای رندر دوباره‌ی صفحه بعد از رد شدن ثبت سفارش).
    آدرس پیش‌انتخاب: آدرس درخواست‌شده (?address=، فقط اگر مالِ همین کاربر باشد)، وگرنه آدرس پیش‌فرض.
    """
    products = [item.product for item in cart.items.all()]
    selected = selected_address or request.user.default_address
    return {
        'cart': cart,
        # روش پرداختی که فرم در اولین رندر تیک می‌زند؛ ردیف‌های سبد باید با همین قیمت‌گذاری
        # شوند تا با باکس فاکتور (UpdateInvoiceView) اختلاف نداشته باشند
        'method': default_payment_method(request.user),
        'address_options': address_options(request.user, products, SiteSettings.cached()),
        'selected_address_id': selected.pk if selected else None,
        'error': error,
    }


class CheckoutView(LoginRequiredMixin, TemplateView):
    """ نمایش صفحه تسویه حساب """
    template_name = 'orders/checkout.html'

    def get(self, request, *args, **kwargs):
        cart = Cart.objects.filter(user=request.user).prefetch_related('items__product').first()
        # اگر سبد خریدی وجود نداشت یا خالی بود، برگرد به صفحه محصولات
        if not cart or cart.items.count() == 0:
            return redirect('products:product_list')
        # بعد از «افزودن آدرس جدید» از همین صفحه، کاربر با آدرسِ تازه پیش‌انتخاب برمی‌گردد
        preselected = get_user_address(request.user, request.GET.get('address'))
        return render(request, self.template_name, build_checkout_context(request, cart, preselected))


class UpdateInvoiceView(LoginRequiredMixin, TemplateView):
    """ ویوی مخصوص HTMX برای محاسبه لایو فاکتور هنگام تغییر روش پرداخت یا آدرس """
    template_name = 'orders/partials/invoice.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        method = resolve_payment_method(self.request.user, self.request.GET.get('payment_method', 'cash'))
        cart = get_object_or_404(Cart, user=self.request.user)
        cart_items = list(cart.items.select_related('product'))

        # آدرس با فیلتر مالک؛ ناموجود/مال دیگری/انتخاب‌نشده ← quote «آدرس ندارد» (مسدود)
        address = get_user_address(self.request.user, self.request.GET.get('address_id'))
        quote = shipping_quote(address, [item.product for item in cart_items], SiteSettings.cached())

        total_items_price = sum(
            final_price(item.product, self.request.user, method) * item.quantity
            for item in cart_items
        )

        context.update({
            'total_items_price': total_items_price,
            'quote': quote,
            'shipping_cost': quote.cost,
            'final_total': total_items_price + quote.cost,
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
        cart_items = list(cart.items.select_related('product').prefetch_related('product__discounts'))

        form = CheckoutForm(request.POST)
        form.is_valid()                                   # فرم فقط رشته‌های پاک‌شده می‌دهد و خطای سخت ندارد

        # ۰. آدرس: شناسه‌ی ارسالی فقط وقتی معتبر است که مالِ همین کاربر باشد (بقیه: ناموجود/مال دیگری/تغییرشده)
        raw_address_id = form.cleaned_data['address_id']
        address = get_user_address(request.user, raw_address_id)
        if raw_address_id and address is None:
            return render(request, self.template_name,
                          build_checkout_context(request, cart, error='آدرس انتخاب‌شده معتبر نیست؛ لطفاً دوباره یکی از آدرس‌های خود را انتخاب کنید.'),
                          status=400)

        # ۱. کرایه و روش ارسال *فقط* از روی آدرسِ دیتابیس و تنظیمات سایت حساب می‌شود؛ آدرسِ مسدود
        # (بدون آدرس، تعرفه‌ی تنظیم‌نشده، ناحیه‌ی ناقص، پس‌کرایه‌ی غیرفعال) هرگز سفارش نمی‌شود
        quote = shipping_quote(address, [item.product for item in cart_items], SiteSettings.cached())
        if not quote.available:
            return render(request, self.template_name,
                          build_checkout_context(request, cart, selected_address=address, error=quote.message))

        method = resolve_payment_method(request.user, form.cleaned_data['payment_method'])

        # ۲. قیمت هر ردیف دقیقاً یک بار محاسبه می‌شود و همان مقدار هم در جمع فاکتور و هم در
        # OrderItem.price می‌نشیند؛ قبلاً دو بار جدا محاسبه می‌شد و اگر تخفیف محصول دقیقاً بین
        # این دو محاسبه منقضی می‌شد، جمع فاکتور با مجموع ردیف‌هایش نمی‌خواند.
        priced_items = [
            (item, final_price(item.product, request.user, method))
            for item in cart_items
        ]
        total_items_price = sum(price * item.quantity for item, price in priced_items)
        final_total = total_items_price + quote.cost

        # ۳. ساخت سفارش با اسنپ‌شات کامل گیرنده/مقصد/ارسال (تغییرات بعدیِ آدرس یا تعرفه فاکتور را عوض نمی‌کند)
        order = Order.objects.create(
            user=request.user,
            payment_method=method,
            total_price=final_total,
            **order_snapshot(address, quote),
        )

        # ۴. کپی کردن آیتم‌ها و قفل کردن قیمتِ همان لحظه (همان قیمتی که بالا جمع زده شد)
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

        # ۵. پاک کردن سبد خرید
        cart.delete()

        # ۶. اعلام رویداد «سفارش ثبت شد».
        # این اپ نمی‌داند و لازم نیست بداند چه کسی به این رویداد گوش می‌دهد (ثبت فاکتور در
        # حسابداری، اطلاع‌رسانی، هر چیز دیگر). با on_commit تا زمانی که تراکنش واقعاً commit
        # نشده رویداد منتشر نمی‌شود؛ وگرنه شنونده‌ای که سفارش را از دیتابیس می‌خواند با
        # DoesNotExist مواجه می‌شود. send_robust تا خطای یک شنونده مسیر کاربر را نشکند.
        transaction.on_commit(lambda: order_placed.send_robust(sender=Order, order=order))

        # ۷. هدایت به صفحه موفقیت
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
            add_item(cart, product, color_id=color_id)
        elif action == 'decrease':
            decrease_item(cart, product, color_id=color_id)

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
    