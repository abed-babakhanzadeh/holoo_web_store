from django.shortcuts import get_object_or_404, render
from django.contrib.auth.mixins import LoginRequiredMixin
from django.views import View
from django.views.generic import TemplateView
from products.models import Product
from .models import Cart, CartItem

class AddToCartView(LoginRequiredMixin, View):
    """ افزودن کالا به سبد خرید و افزایش تعداد """
    
    # تعریف متد post به صورت خودکار کارِ require_POST را انجام می‌دهد
    def post(self, request, product_id, *args, **kwargs):
        product = get_object_or_404(Product, id=product_id, is_active=True)
        cart, _ = Cart.objects.get_or_create(user=request.user)
        cart_item, created = CartItem.objects.get_or_create(cart=cart, product=product)

        if not created and cart_item.quantity < product.stock:
            cart_item.quantity += 1
            cart_item.save()

        # ارسال سیگنال آپدیت به مینی‌کارت
        compact = request.POST.get('compact') == 'true'
        response = render(request, 'cart/partials/cart_button.html', {'product': product, 'cart_item': cart_item, 'compact': compact})
        response['HX-Trigger'] = 'cartUpdated'
        return response


class DecreaseCartView(LoginRequiredMixin, View):
    """ کاهش تعداد کالا یا حذف کامل آن از سبد خرید """

    def post(self, request, product_id, *args, **kwargs):
        product = get_object_or_404(Product, id=product_id)
        cart_item = None
        try:
            cart = Cart.objects.get(user=request.user)
            cart_item = CartItem.objects.get(cart=cart, product=product)
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
        response = render(request, 'cart/partials/cart_button.html', {'product': product, 'cart_item': cart_item, 'compact': compact})
        response['HX-Trigger'] = 'cartUpdated'
        return response


class RemoveFromCartView(LoginRequiredMixin, View):
    """ حذف کامل یک کالا از سبد خرید (صرف‌نظر از تعداد)، برای دکمه‌ی × در آفکانواس سبد """

    def post(self, request, product_id, *args, **kwargs):
        try:
            cart = Cart.objects.get(user=request.user)
            CartItem.objects.filter(cart=cart, product_id=product_id).delete()
        except Cart.DoesNotExist:
            pass

        response = render(request, 'cart/partials/nav_cart.html', {'nav_cart': Cart.objects.filter(user=request.user).first()})
        response['HX-Trigger'] = 'cartUpdated'
        return response


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
    