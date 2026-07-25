from .models import Category, SiteSettings


def storefront(request):
    """ داده‌های سراسری قالب (منوی دسته‌بندی‌ها و سبد خرید) که در هدر/آفکانواس همه صفحات لازم است """
    categories = Category.objects.filter(is_active=True, parent__isnull=True).prefetch_related('children').order_by('name')

    from blog.models import BlogCategory
    blog_categories = BlogCategory.objects.filter(is_active=True).order_by('name')[:6]

    nav_cart = None
    if request.user.is_authenticated:
        from cart.models import Cart
        nav_cart = Cart.objects.filter(user=request.user).prefetch_related('items__product').first()

    return {
        'nav_categories': categories,
        'nav_blog_categories': blog_categories,
        'nav_cart': nav_cart,
        'compare_count': len(request.session.get('compare_ids', [])),
        'site_settings': SiteSettings.load(),
    }
