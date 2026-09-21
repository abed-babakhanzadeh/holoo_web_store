from django.shortcuts import get_object_or_404, render
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views import View
from django.views.generic import TemplateView
from products.models import Product, ProductColor
from .models import Cart, CartItem
from .services import add_item, decrease_item


def _resolve_color(product, color_id):
    if not color_id:
        return None
    return ProductColor.objects.filter(id=color_id, product=product).first()


class AddToCartView(LoginRequiredMixin, View):
    """ افزودن کالا (با رنگ مشخص) به سبد خرید و افزایش تعداد؛ هر رنگ ردیف جدای خودش را دارد """

    # تعریف متد post به صورت خودکار کارِ require_POST را انجام می‌دهد
    def post(self, request, product_id, *args, **kwargs):
        product = get_object_or_404(Product.visible, id=product_id)
        cart, _ = Cart.objects.get_or_create(user=request.user)
        color = _resolve_color(product, request.POST.get('color_id'))

        # اگر محصول رنگ‌بندی دارد، انتخاب رنگ الزامی است؛ بدون آن به سبد اضافه نمی‌شود
        if product.colors.exists() and color is None:
            cart_item = None
        else:
            cart_item = add_item(cart, product, color_id=color.id if color else None)

        # ارسال سیگنال آپدیت به مینی‌کارت
        compact = request.POST.get('compact') == 'true'
        response = render(request, 'cart/partials/cart_button.html', {
            'product': product, 'cart_item': cart_item, 'compact': compact, 'selected_color_id': color.id if color else '',
        })
        response['HX-Trigger'] = 'cartUpdated'
        return response


class DecreaseCartView(LoginRequiredMixin, View):
    """ کاهش تعداد کالا (برای رنگ مشخص) یا حذف کامل آن ردیف از سبد خرید """

    def post(self, request, product_id, *args, **kwargs):
        product = get_object_or_404(Product, id=product_id)
        color = _resolve_color(product, request.POST.get('color_id'))
        cart = Cart.objects.filter(user=request.user).first()
        cart_item = decrease_item(cart, product, color_id=color.id if color else None) if cart else None

        # ارسال سیگنال آپدیت به مینی‌کارت
        compact = request.POST.get('compact') == 'true'
        response = render(request, 'cart/partials/cart_button.html', {
            'product': product, 'cart_item': cart_item, 'compact': compact, 'selected_color_id': color.id if color else '',
        })
        response['HX-Trigger'] = 'cartUpdated'
        return response


class RemoveFromCartView(LoginRequiredMixin, View):
    """ حذف کامل یک ردیف (محصول+رنگ) از سبد خرید، برای دکمه‌ی × در آفکانواس سبد """

    def post(self, request, product_id, *args, **kwargs):
        try:
            cart = Cart.objects.get(user=request.user)
            color_id = request.POST.get('color_id') or None
            CartItem.objects.filter(cart=cart, product_id=product_id, color_id=color_id).delete()
        except Cart.DoesNotExist:
            pass

        response = render(request, 'cart/partials/nav_cart.html', {'nav_cart': _cart_with_items(request.user)})
        response['HX-Trigger'] = 'cartUpdated'
        return response


class CartButtonStatusView(LoginRequiredMixin, View):
    """ برای هماهنگ نگه‌داشتن دکمه‌ی سبد خرید محصول (مخصوص رنگ انتخابی) با تغییراتی که از جای دیگر رخ می‌دهد """

    def get(self, request, product_id, *args, **kwargs):
        # این پرترافیک‌ترین endpoint سایت است (هر کارت محصول یکی می‌زند): رنگ‌ها را که قالب
        # دکمه لازم دارد همراه محصول می‌آوریم و سبد/آیتم را با یک کوئری (به‌جای دو) می‌خوانیم.
        # تخفیف عمداً prefetch نمی‌شود چون این قالب قیمتی نمایش نمی‌دهد.
        product = get_object_or_404(Product.visible.prefetch_related('colors'), id=product_id)
        color = _resolve_color(product, request.GET.get('color_id'))
        cart_item = CartItem.objects.filter(cart__user=request.user, product=product, color=color).first()

        compact = request.GET.get('compact') == 'true'
        return render(request, 'cart/partials/cart_button.html', {
            'product': product, 'cart_item': cart_item, 'compact': compact, 'selected_color_id': color.id if color else '',
        })


def _cart_with_items(user):
    """
    سبد کاربر به‌همراه هر چیزی که قالب آفکانواس/مینی‌کارت لازم دارد، در یک رفت‌وبرگشت.
    بدون prefetch، هر ردیف سبد چند کوئری جدا می‌زد (محصول، رنگ، و تخفیف فعال برای محاسبه‌ی قیمت).
    """
    cart, _ = Cart.objects.get_or_create(user=user)
    return (
        Cart.objects.filter(pk=cart.pk)
        .select_related('user')  # get_cost به cart.user نیاز دارد
        .prefetch_related('items__product', 'items__color')
        .first()
    )


class MiniCartView(LoginRequiredMixin, TemplateView):
    """ این ویو فقط برای لود کردن محتوای مینی‌کارت شناور است """
    template_name = 'cart/partials/mini_cart.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['cart'] = _cart_with_items(self.request.user)
        return context


class NavCartView(LoginRequiredMixin, TemplateView):
    """ بج تعداد سبد خرید در هدر و محتوای آفکانواس سبد را با شنیدن رویداد cartUpdated به‌روز نگه می‌دارد """
    template_name = 'cart/partials/nav_cart.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['nav_cart'] = _cart_with_items(self.request.user)
        return context
