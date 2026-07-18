from django.core.paginator import Paginator
from django.shortcuts import render
from django.views import View
from .models import Product, Category
from django.views.generic import DetailView
from recently_viewed.models import RecentlyViewed

PRODUCTS_PER_PAGE = 12


class HomeView(View):
    """ ویوی صفحه اصلی (ویترین) فروشگاه """

    def get(self, request, *args, **kwargs):
        products = Product.objects.filter(is_active=True).select_related('category').order_by('-created_at')[:8]
        categories = Category.objects.filter(is_active=True, parent__isnull=True).prefetch_related('children')
        context = {
            'products': products,
            'categories': categories,
        }
        return render(request, 'products/home.html', context)


class ProductListView(View):
    """ ویوی نمایش فروشگاه کامل، جستجوی زنده و فیلتر دسته‌بندی‌ها """

    def get(self, request, *args, **kwargs):
        # ۱. دریافت تمام محصولات فعال (جدیدترین‌ها در ابتدا)
        products = Product.objects.filter(is_active=True).select_related('category')

        # ۲. دریافت دسته‌بندی‌های اصلی (آن‌هایی که پدر ندارند) برای سایدبار
        categories = Category.objects.filter(is_active=True, parent__isnull=True).prefetch_related('children')

        # ۳. اعمال فیلتر جستجوی متنی (تایپ زنده)
        search_query = request.GET.get('q', '').strip()
        if search_query:
            products = products.filter(name__icontains=search_query)

        # ۴. اعمال فیلتر دسته‌بندی
        category_slug = request.GET.get('category')
        if category_slug:
            # اگر دسته‌بندی انتخاب شد، هم خودش و هم زیردسته‌هایش را فیلتر کن
            products = products.filter(
                category__slug=category_slug
            ) | products.filter(
                category__parent__slug=category_slug
            )

        # ۵. صفحه‌بندی نتایج
        paginator = Paginator(products.distinct(), PRODUCTS_PER_PAGE)
        page_number = request.GET.get('page', 1)
        page_obj = paginator.get_page(page_number)

        context = {
            'products': page_obj,
            'page_obj': page_obj,
            'categories': categories,
            'current_category': category_slug,
            'search_query': search_query,
        }

        # ۶. جادوی HTMX: اگر درخواست از سمت HTMX بود، فقط گرید محصولات را برگردان
        if request.headers.get('HX-Request'):
            return render(request, 'products/partials/product_grid.html', context)

        # در غیر این صورت، کل صفحه را با قالب اصلی برگردان
        return render(request, 'products/product_list.html', context)
    
class LiveSearchView(View):
    """ جستجوی زنده‌ی هدر: چند نتیجه‌ی سریع زیر کادر جستجو، بدون رفتن به صفحه‌ی دیگر """

    def get(self, request, *args, **kwargs):
        query = request.GET.get('q', '').strip()
        products = []
        if query:
            products = Product.objects.filter(is_active=True, name__icontains=query).select_related('category')[:6]
        return render(request, 'products/partials/live_search_results.html', {'query': query, 'products': products})


class ProductDetailView(DetailView):
    """ ویوی نمایش جزئیات کامل یک محصول و مشخصات فنی آن """
    model = Product
    template_name = 'products/product_detail.html'
    context_object_name = 'product'
    
    def get_queryset(self):
        # فقط محصولات فعال اجازه نمایش دارند
        return Product.objects.filter(is_active=True).select_related('category')
        
    def get(self, request, *args, **kwargs):
        response = super().get(request, *args, **kwargs)
        if request.user.is_authenticated:
            RecentlyViewed.track(user=request.user, product=self.object)
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # واکشی مشخصات فنی (EAV) مربوط به همین محصول بهینه‌سازی شده با select_related
        context['features'] = self.object.features.select_related('feature').all()
        return context