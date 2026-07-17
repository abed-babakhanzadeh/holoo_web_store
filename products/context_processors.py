from .models import Category


def storefront(request):
    """ داده‌های سراسری قالب (منوی دسته‌بندی‌ها و سبد خرید) که در هدر/آفکانواس همه صفحات لازم است """
    categories = Category.objects.filter(is_active=True, parent__isnull=True).prefetch_related('children').order_by('name')

    nav_cart = None
    if request.user.is_authenticated:
        from cart.models import Cart
        nav_cart = Cart.objects.filter(user=request.user).prefetch_related('items__product').first()

    return {
        'nav_categories': categories,
        'nav_cart': nav_cart,
    }
