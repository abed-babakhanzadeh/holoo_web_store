from django.shortcuts import get_object_or_404, render
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from products.models import Product
from .models import Cart, CartItem

@login_required
@require_POST
def add_to_cart(request, product_id):
    product = get_object_or_404(Product, id=product_id, is_active=True)
    cart, _ = Cart.objects.get_or_create(user=request.user)
    cart_item, created = CartItem.objects.get_or_create(cart=cart, product=product)

    if not created and cart_item.quantity < product.stock:
        cart_item.quantity += 1
        cart_item.save()

    # ارسال سیگنال آپدیت به مینی‌کارت
    response = render(request, 'cart/partials/cart_button.html', {'product': product, 'cart_item': cart_item})
    response['HX-Trigger'] = 'cartUpdated'
    return response

@login_required
@require_POST
def decrease_cart(request, product_id):
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
    response = render(request, 'cart/partials/cart_button.html', {'product': product, 'cart_item': cart_item})
    response['HX-Trigger'] = 'cartUpdated'
    return response

@login_required
def mini_cart(request):
    """ این ویو فقط برای لود کردن محتوای مینی‌کارت شناور است """
    cart, _ = Cart.objects.get_or_create(user=request.user)
    return render(request, 'cart/partials/mini_cart.html', {'cart': cart})