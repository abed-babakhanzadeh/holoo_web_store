from datetime import timedelta
from decimal import Decimal
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
from products.pricing import default_payment_method, resolve_payment_method
from cart.models import Cart
from cart.pricing import price_cart
from cart.services import add_item, decrease_item
from promotions import coupons, free_shipping, ratelimit
from promotions.models import normalize_code
from .checkout import address_options, compute_checkout, get_user_address
from .forms import CheckoutForm
from .models import Order, OrderItem
from .signals import order_placed
from .snapshot import order_snapshot


COUPON_KEY = coupons.SESSION_KEY

PRICE_DRIFT_MESSAGE = ('مبالغ سفارش شما به‌دلیل تغییر وضعیت تخفیف‌ها به‌روز شد؛ '
                       'لطفاً بررسی و تأیید نهایی نمایید.')


class CheckoutApprovalRequiredMixin(LoginRequiredMixin):
    """
    مثل LoginRequiredMixin ولی علاوه بر ورود، accounts.CustomUser.can_order() هم لازم است
    (چرخه‌ی تأیید تجاری، فاز ۲/۳). گیت Fail-Closed سمت سرور: بدون این، کاربرِ واردشده‌ی
    تأییدنشده می‌توانست مستقیماً (با دستکاری URL؛ دکمه‌های UI اصلاً برایش رندر نمی‌شوند) به
    compute_checkout/price_cart برسد که چون قیمتش پنهان است (products.pricing.is_price_hidden)
    base/final را None برمی‌گرداند — یعنی None وارد خط لوله‌ی محاسبات checkout می‌شد. اینجا
    درخواست همان لحظه‌ی ورود به ویو (قبل از هر محاسبه‌ای) متوقف می‌شود.
    """

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not request.user.can_order():
            return redirect('products:home')
        return super().dispatch(request, *args, **kwargs)


def _client_ip(request):
    # عمداً فقط REMOTE_ADDR: هدر X-Forwarded-For را کلاینت می‌تواند جعل کند (پشت پروکسیِ مورداعتماد باید در وب‌سرور تنظیم شود)
    return request.META.get('REMOTE_ADDR', '')


def _parse_amount(text):
    """ مبلغ ارسالیِ فرم (فقط ارقام لاتین) ← Decimal، وگرنه None. فقط برای مقایسه؛ هرگز مبنای مبلغ سفارش نیست """
    text = (text or '').strip()
    return Decimal(text) if text.isascii() and text.isdigit() else None


def invoice_context(cart, method, totals, coupon_message='', coupon_message_kind=''):
    """ زمینه‌ی قالب invoice.html از روی یک CheckoutTotals (تنها منبع مبلغ‌ها) """
    return {
        'totals': totals,
        'pricing': totals.pricing,
        'quote': totals.quote,
        'total_items_price': totals.items_total,
        'coupon': totals.applied_coupon,
        'coupon_discount': totals.coupon_discount,
        'coupon_notice': totals.coupon_notice,
        'coupon_message': coupon_message,
        'coupon_message_kind': coupon_message_kind,
        'shipping_cost': totals.shipping_cost,
        'final_total': totals.final_total,
        'method': method,
        'cart': cart,
    }


def build_checkout_context(request, cart, selected_address=None, error=None, items=None, selected_method=None):
    """
    زمینه‌ی صفحه‌ی تسویه‌حساب (هم برای نمایش اول و هم برای رندر دوباره‌ی صفحه بعد از رد شدن ثبت سفارش).
    آدرس پیش‌انتخاب: آدرس درخواست‌شده (?address=، فقط اگر مالِ همین کاربر باشد)، وگرنه آدرس پیش‌فرض.
    """
    items = list(cart.items.all()) if items is None else items
    products = [item.product for item in items]
    selected = selected_address or request.user.default_address
    # روش پرداختی که فرم در اولین رندر تیک می‌زند؛ ردیف‌های سبد باید با همین قیمت‌گذاری
    # شوند تا با باکس فاکتور (UpdateInvoiceView) اختلاف نداشته باشند
    method = resolve_payment_method(request.user, selected_method) if selected_method else default_payment_method(request.user)
    pricing = price_cart(items, request.user, method)
    return {
        'cart': cart,
        'pricing': pricing,
        'method': method,
        'selected_method': method,
        'address_options': address_options(request.user, products, SiteSettings.cached(),
                                          cart_total=pricing.items_total, free_rules=free_shipping.enabled_rules(), now=pricing.now),
        'selected_address_id': selected.pk if selected else None,
        'error': error,
    }


class CheckoutView(CheckoutApprovalRequiredMixin, TemplateView):
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


class InvoiceMixin:
    """ محاسبه‌ی فاکتور زنده از روی نشست (کد تخفیف)، آدرس و روش پرداخت؛ مشترک بین نمایش، اعمال و حذف کد """
    template_name = 'orders/partials/invoice.html'

    def render_invoice(self, request, params, coupon_message='', coupon_message_kind=''):
        method = resolve_payment_method(request.user, params.get('payment_method', 'cash'))
        cart = get_object_or_404(Cart, user=request.user)
        cart_items = list(cart.items.select_related('product'))
        # آدرس با فیلتر مالک؛ ناموجود/مال دیگری/انتخاب‌نشده ← quote «آدرس ندارد» (مسدود)
        address = get_user_address(request.user, params.get('address_id'))
        totals = compute_checkout(request.user, cart_items, method, address, request.session.get(COUPON_KEY, ''),
                                  SiteSettings.cached())
        return render(request, self.template_name, invoice_context(cart, method, totals, coupon_message, coupon_message_kind))


class UpdateInvoiceView(InvoiceMixin, CheckoutApprovalRequiredMixin, View):
    """ ویوی مخصوص HTMX برای محاسبه لایو فاکتور هنگام تغییر روش پرداخت، آدرس یا سبد """

    def get(self, request, *args, **kwargs):
        return self.render_invoice(request, request.GET)


class ApplyCouponView(InvoiceMixin, CheckoutApprovalRequiredMixin, View):
    """
    اعمال کد تخفیف (HTMX). کد فقط پس از ارزیابیِ موفق در نشست ذخیره می‌شود و فاکتور زنده دوباره رندر می‌شود.
    حدس‌های ناموفق (کد ناموجود/غیرفعال/منقضی/تعریف‌نشده) برای کاربر و IP شمرده می‌شوند و از سقفی به بعد مسدودند.
    """

    def post(self, request, *args, **kwargs):
        code = normalize_code(request.POST.get('code'))
        if not code:
            return self.render_invoice(request, request.POST, coupons.MESSAGES[coupons.EMPTY_CODE], 'error')

        ip = _client_ip(request)
        allowed, retry_after = ratelimit.check(request.user, ip)
        if not allowed:
            minutes = max(1, -(-retry_after // 60))
            return self.render_invoice(
                request, request.POST,
                f'تعداد تلاش‌های ناموفق شما از حد مجاز گذشته است؛ حدود {minutes} دقیقه‌ی دیگر دوباره تلاش کنید.', 'error',
            )

        method = resolve_payment_method(request.user, request.POST.get('payment_method', 'cash'))
        cart = get_object_or_404(Cart, user=request.user)
        cart_items = list(cart.items.select_related('product'))
        address = get_user_address(request.user, request.POST.get('address_id'))
        totals = compute_checkout(request.user, cart_items, method, address, code, SiteSettings.cached())
        result = totals.coupon

        if result is not None and result.ok:
            request.session[COUPON_KEY] = result.code
            return self.render_invoice(request, request.POST, self._success_message(result), 'success')
        if result is not None and result.is_guess_failure:
            ratelimit.record_failure(request.user, ip)
        return self.render_invoice(request, request.POST, result.message if result else coupons.MESSAGES[coupons.NOT_FOUND], 'error')

    @staticmethod
    def _success_message(result):
        if result.free_shipping:
            return f'کد «{result.code}» اعمال شد؛ ارسال این سفارش رایگان است.'
        text = f'کد «{result.code}» اعمال شد.'
        if result.excluded_lines:
            text += f' (روی {result.excluded_lines} قلم دارای تخفیف خودکار اعمال نمی‌شود.)'
        return text


class RemoveCouponView(InvoiceMixin, CheckoutApprovalRequiredMixin, View):
    """ حذف کد تخفیف از نشست (HTMX) و رندر دوباره‌ی فاکتور """

    def post(self, request, *args, **kwargs):
        request.session.pop(COUPON_KEY, None)
        return self.render_invoice(request, request.POST, 'کد تخفیف حذف شد.', 'info')


class SubmitOrderView(CheckoutApprovalRequiredMixin, View):
    """
    ثبت نهایی، قفل کردن قیمت‌ها، پاک کردن سبد و اعلام رویداد ثبت سفارش.

    همه‌ی مراحل در *یک* تراکنش اتمیک‌اند: قفل سبد ← قفل ردیف کوپن ← محاسبه‌ی دوباره‌ی همه‌ی مبلغ‌ها از دیتابیس با ساعت
    سرور ← کنترل مغایرت با expected_total ← ساخت سفارش و اسنپ‌شات ← رزرو ظرفیت کد. هر خطا/توقفی قبل از نوشتن اتفاق
    می‌افتد یا کل تراکنش برمی‌گردد؛ سفارشِ نیمه‌کاره یا کدِ مصرف‌شده‌ی بی‌سفارش ساخته نمی‌شود.

    (کسر موجودی در جریان فعلیِ پروژه وجود ندارد؛ موجودی مالِ هلوست و طراحی آن هنوز مسدود است.)
    """
    template_name = 'orders/checkout.html'

    @method_decorator(transaction.atomic)
    def post(self, request, *args, **kwargs):
        # قفل سبد: کلیک دوباره/درخواست هم‌زمان پشت این قفل صف می‌شود و دوباره سفارش نمی‌سازد (سبد پس از سفارش پاک می‌شود)
        carts = list(Cart.objects.select_for_update().filter(user=request.user))
        if not carts:
            return redirect('orders:order_history')
        cart = carts[0]
        cart_items = list(cart.items.select_related('product'))
        if not cart_items:
            return redirect('products:product_list')

        form = CheckoutForm(request.POST)
        form.is_valid()                                   # فرم فقط رشته‌های پاک‌شده می‌دهد و خطای سخت ندارد

        # ۰. آدرس: شناسه‌ی ارسالی فقط وقتی معتبر است که مالِ همین کاربر باشد (بقیه: ناموجود/مال دیگری/تغییرشده)
        raw_address_id = form.cleaned_data['address_id']
        address = get_user_address(request.user, raw_address_id)
        method = resolve_payment_method(request.user, form.cleaned_data['payment_method'])
        if raw_address_id and address is None:
            return render(request, self.template_name,
                          build_checkout_context(request, cart, items=cart_items, selected_method=method,
                                                 error='آدرس انتخاب‌شده معتبر نیست؛ لطفاً دوباره یکی از آدرس‌های خود را انتخاب کنید.'),
                          status=400)

        # ۱. محاسبه‌ی کامل و *دوباره* از روی دیتابیس (اقلام، تخفیف‌ها، کد تخفیف، ارسال) با یک «اکنونِ سرور». ردیف کوپن
        # همین‌جا قفل می‌شود و ظرفیتش داخل همین قفل سنجیده می‌شود. کرایه و روش ارسال فقط از آدرسِ دیتابیس و تنظیمات سایت
        # می‌آید؛ آدرسِ مسدود (بدون آدرس، تعرفه‌ی تنظیم‌نشده، ناحیه‌ی ناقص، پس‌کرایه‌ی غیرفعال) هرگز سفارش نمی‌شود
        totals = compute_checkout(request.user, cart_items, method, address, request.session.get(COUPON_KEY, ''),
                                  SiteSettings.cached(), lock_coupon=True)
        if not totals.quote.available:
            return render(request, self.template_name,
                          build_checkout_context(request, cart, selected_address=address, error=totals.quote.message,
                                                 items=cart_items, selected_method=method))

        # ۲. کنترل نوسان قیمت: مبلغی که کاربر دیده با محاسبه‌ی سرور باید *دقیقاً* یکی باشد (حتی ۱ ریال). مقدار ارسالی
        # فقط مقایسه می‌شود و به هیچ‌وجه مبنای مبلغ سفارش نیست. غیبتِ مقدار هم مغایرت است.
        expected = _parse_amount(form.cleaned_data['expected_total'])
        if expected is None or expected != totals.final_total:
            return self._price_drift_response(request, cart, cart_items, address, method, totals)

        # ۳. ساخت سفارش با اسنپ‌شات کامل گیرنده/مقصد/ارسال/تخفیف (تغییرات بعدیِ آدرس، تعرفه یا کمپین‌ها فاکتور
        # را عوض نمی‌کند). مبلغ‌ها همه از totals می‌آیند که همین لحظه در همین تراکنش حساب شد.
        applied = totals.applied_coupon
        order = Order.objects.create(
            user=request.user,
            payment_method=method,
            total_price=totals.final_total,
            promotion_discount=totals.pricing.promotion_discount,
            order_discount=totals.coupon_discount,
            order_discount_label=applied.order_label if applied and totals.coupon_discount > 0 else '',
            coupon_code=applied.code if applied else '',
            **order_snapshot(address, totals.quote),
        )

        # ۴. کپی کردن آیتم‌ها و قفل کردن قیمتِ همان لحظه (همان قیمتی که بالا جمع زده شد) به‌همراه قیمت
        # اصلی و تخفیف هر واحد
        OrderItem.objects.bulk_create([
            OrderItem(
                order=order,
                product=line.product,
                color=line.item.color,
                price=line.unit_final,
                original_price=line.unit_original,
                discount_amount=line.unit_discount,
                quantity=line.quantity,
            )
            for line in totals.pricing.lines
        ])

        # ۵. رزرو ظرفیت کد (داخل همان قفلی که ردیف کوپن را گرفته؛ درخواست هم‌زمان دیگر پشت آن منتظر است)
        if applied is not None:
            coupons.reserve(applied.coupon, request.user, order.id, applied, totals.now)
            request.session.pop(COUPON_KEY, None)

        # ۶. پاک کردن سبد خرید
        cart.delete()

        # ۷. اعلام رویداد «سفارش ثبت شد».
        # این اپ نمی‌داند و لازم نیست بداند چه کسی به این رویداد گوش می‌دهد (ثبت فاکتور در
        # حسابداری، اطلاع‌رسانی، هر چیز دیگر). با on_commit تا زمانی که تراکنش واقعاً commit
        # نشده رویداد منتشر نمی‌شود؛ وگرنه شنونده‌ای که سفارش را از دیتابیس می‌خواند با
        # DoesNotExist مواجه می‌شود. send_robust تا خطای یک شنونده مسیر کاربر را نشکند.
        transaction.on_commit(lambda: order_placed.send_robust(sender=Order, order=order))

        # ۸. هدایت به صفحه موفقیت
        return redirect('orders:order_success', order_id=order.id)

    def _price_drift_response(self, request, cart, cart_items, address, method, totals):
        """
        مبلغ محاسبه‌ی سرور با آنچه کاربر دیده فرق دارد: هیچ سفارشی ساخته نمی‌شود؛ صفحه‌ی تسویه با فاکتور جدید،
        expected_total به‌روز و پیام هشدار دوباره نمایش داده می‌شود (وضعیت ۴۰۹ = تعارض).
        """
        notice = PRICE_DRIFT_MESSAGE
        if totals.coupon_notice:
            notice += f' کد تخفیف شما دیگر قابل اعمال نیست: {totals.coupon_notice}'
        context = build_checkout_context(request, cart, selected_address=address, items=cart_items, selected_method=method)
        context.update(invoice_context(cart, method, totals))
        context.update({'show_invoice': True, 'skip_items_oob': True, 'price_drift_notice': notice})
        return render(request, self.template_name, context, status=409)


class OrderSuccessView(LoginRequiredMixin, TemplateView):
    """ نمایش صفحه موفقیت سفارش """
    template_name = 'orders/success.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # order_id از طریق URL به این متد پاس داده می‌شود
        context['order'] = get_object_or_404(Order, id=self.kwargs['order_id'], user=self.request.user)
        return context
    
class CheckoutCartUpdateView(CheckoutApprovalRequiredMixin, View):
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
        pricing = price_cart(cart.items.select_related('product'), request.user, method)
        response = render(request, 'orders/partials/checkout_cart_items.html', {'cart': cart, 'pricing': pricing, 'method': method})
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
        # ردیف‌ها یک‌بار خوانده می‌شوند و همه‌ی جمع‌ها از همان‌ها می‌آیند
        items = list(order.items.select_related('product'))
        context['items'] = items
        context['items_subtotal'] = sum((item.get_cost() for item in items), Decimal('0'))
        context['items_original_total'] = sum((item.original_cost for item in items), Decimal('0'))
        context['paid_transaction'] = order.transactions.filter(status='success').order_by('-created_at').first()
        return context
    