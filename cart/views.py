from django.shortcuts import get_object_or_404, render
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views import View
from django.views.generic import TemplateView
from products.models import Product, ProductColor
from .models import Cart, CartItem


def _resolve_color(product, color_id):
    if not color_id:
        return None
    return ProductColor.objects.filter(id=color_id, product=product).first()


class AddToCartView(LoginRequiredMixin, View):
    """ افزودن کالا (با رنگ مشخص) به سبد خرید و افزایش تعداد؛ هر رنگ ردیف جدای خودش را دارد """

    # تعریف متد post به صورت خودکار کارِ require_POST را انجام می‌دهد
    def post(self, request, product_id, *args, **kwargs):
        product = get_object_or_404(Product, id=product_id, is_active=True)
        cart, _ = Cart.objects.get_or_create(user=request.user)
        color = _resolve_color(product, request.POST.get('color_id'))

        cart_item, created = CartItem.objects.get_or_create(cart=cart, product=product, color=color)

        if not created and cart_item.quantity < product.stock:
            cart_item.quantity += 1
            cart_item.save()

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
        cart_item = None
        try:
            cart = Cart.objects.get(user=request.user)
            cart_item = CartItem.objects.get(cart=cart, product=product, color=color)
            if cart_item.quantity > 1:
                cart_item.quantity -= 1
                cart_item.save()
            else:
                cart_item.delete()
                cart_item = None # کالا کامل از سبد حذف شد
        except (Cart.DoesNotExist, CartItem.DoesNotExist):
            pass

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

        response = render(request, 'cart/partials/nav_cart.html', {'nav_cart': Cart.objects.filter(user=request.user).first()})
        response['HX-Trigger'] = 'cartUpdated'
        return response


class CartButtonStatusView(LoginRequiredMixin, View):
    """ برای هماهنگ نگه‌داشتن دکمه‌ی سبد خرید محصول (مخصوص رنگ انتخابی) با تغییراتی که از جای دیگر رخ می‌دهد """

    def get(self, request, product_id, *args, **kwargs):
        product = get_object_or_404(Product, id=product_id, is_active=True)
        color = _resolve_color(product, request.GET.get('color_id'))
        cart_item = None
        try:
            cart = Cart.objects.get(user=request.user)
            cart_item = CartItem.objects.get(cart=cart, product=product, color=color)
        except (Cart.DoesNotExist, CartItem.DoesNotExist):
            pass

        compact = request.GET.get('compact') == 'true'
        return render(request, 'cart/partials/cart_button.html', {
            'product': product, 'cart_item': cart_item, 'compact': compact, 'selected_color_id': color.id if color else '',
        })


class MiniCartView(LoginRequiredMixin, TemplateView):
    """ این ویو فقط برای لود کردن محتوای مینی‌کارت شناور است """
    template_name = 'cart/partials/mini_cart.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        cart, _ = Cart.objects.get_or_create(user=self.request.user)
        context['cart'] = cart
        return context


class NavCartView(LoginRequiredMixin, TemplateView):
    """ بج تعداد سبد خرید در هدر و محتوای آفکانواس سبد را با شنیدن رویداد cartUpdated به‌روز نگه می‌دارد """
    template_name = 'cart/partials/nav_cart.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        cart, _ = Cart.objects.get_or_create(user=self.request.user)
        context['nav_cart'] = cart
        return context
