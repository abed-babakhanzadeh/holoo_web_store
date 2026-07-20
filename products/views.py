from django.core.paginator import Paginator
from django.db.models import Avg, Count, Min, Max, Sum, Q
from django.shortcuts import render
from django.views import View
from .models import Product, Category, Brand, ProductColor
from django.views.generic import DetailView
from recently_viewed.models import RecentlyViewed
from reviews.models import Review

REVIEW_SORT_OPTIONS = {
    'newest': ('-created_at',),
    'oldest': ('created_at',),
    'rating_high': ('-rating', '-created_at'),
    'rating_low': ('rating', '-created_at'),
}

PRODUCTS_PER_PAGE = 12

# ترتیب‌های مجاز فروشگاه؛ کلید = مقدار پارامتر sort در URL، مقدار = برچسب نمایشی
PRODUCT_SORT_OPTIONS = (
    ('newest', 'جدیدترین'),
    ('price_asc', 'ارزان‌ترین'),
    ('price_desc', 'گران‌ترین'),
    ('best_selling', 'پرفروش‌ترین'),
    ('most_viewed', 'پربازدیدترین'),
    ('top_rated', 'بیشترین امتیاز'),
)
PRODUCT_SORT_VALUES = {key for key, _ in PRODUCT_SORT_OPTIONS}
# سفارش‌هایی که «فروش واقعی‌شده» حساب می‌شوند (لغوشده و در انتظار پرداخت حساب نمی‌شوند)
SOLD_ORDER_STATUSES = ('registered', 'processing', 'shipped', 'delivered')


def _apply_sort(products, sort):
    """ اعمال ترتیب روی کوئری‌ست محصولات؛ برای گزینه‌های آماری، annotate لازم انجام می‌شود """
    if sort == 'price_asc':
        return products.order_by('price', 'id')
    if sort == 'price_desc':
        return products.order_by('-price', 'id')
    if sort == 'best_selling':
        return products.annotate(
            sold_count=Sum('order_items__quantity', filter=Q(order_items__order__status__in=SOLD_ORDER_STATUSES))
        ).order_by('-sold_count', '-created_at', 'id')
    if sort == 'most_viewed':
        return products.annotate(
            view_count=Count('recently_viewed_by', distinct=True)
        ).order_by('-view_count', '-created_at', 'id')
    if sort == 'top_rated':
        return products.annotate(
            avg_rating=Avg('reviews__rating', filter=Q(reviews__status='published', reviews__parent__isnull=True))
        ).order_by('-avg_rating', '-created_at', 'id')
    return products  # 'newest' -> ترتیب پیش‌فرض کوئری‌ست پایه (-created_at) از قبل درسته


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


def _parse_price(value):
    """ تبدیل امن مقدار قیمت ارسالی از کوئری‌استرینگ؛ مقدار نامعتبر را نادیده می‌گیرد """
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class ProductListView(View):
    """ ویوی نمایش فروشگاه کامل، جستجوی زنده و فیلتر دسته‌بندی‌ها/قیمت/رنگ/برند """

    def get(self, request, *args, **kwargs):
        # ۱. دریافت تمام محصولات فعال (جدیدترین‌ها در ابتدا)
        # ترتیب صریح لازم است تا Paginator نتایج پایدار بدهد (بدون order_by ترتیب ردیف‌ها
        # در MSSQL تضمین‌شده نیست و بین صفحات ممکن است آیتم‌ها جابه‌جا/تکراری شوند)
        products = Product.objects.filter(is_active=True).select_related('category').order_by('-created_at', 'id')

        # ۲. دریافت دسته‌بندی‌های اصلی (آن‌هایی که پدر ندارند) برای سایدبار
        categories = Category.objects.filter(is_active=True, parent__isnull=True).prefetch_related('children')

        # ۳. اعمال فیلتر جستجوی متنی (تایپ زنده)
        search_query = request.GET.get('q', '').strip()
        if search_query:
            products = products.filter(name__icontains=search_query)

        # ۴. اعمال فیلتر دسته‌بندی (خودش + همه‌ی زیردسته‌ها در هر عمقی، نه فقط یک سطح)
        category_slug = request.GET.get('category')
        if category_slug:
            selected_category = Category.objects.filter(slug=category_slug).first()
            if selected_category:
                products = products.filter(category_id__in=selected_category.get_descendant_ids())
            else:
                products = products.none()

        # ۴.۵. اعمال فیلتر برند (چندتایی؛ سازگار با لینک تک‌برندی «محصولات دیگر این برند» در صفحه محصول)
        brand_slugs = request.GET.getlist('brand')
        if brand_slugs:
            products = products.filter(brand__slug__in=brand_slugs)

        # ۴.۶. اعمال فیلتر رنگ
        color = request.GET.get('color')
        if color:
            products = products.filter(colors__name=color)

        # ۴.۷. اعمال فیلتر بازه‌ی قیمت
        price_min = _parse_price(request.GET.get('price_min'))
        price_max = _parse_price(request.GET.get('price_max'))
        if price_min is not None:
            products = products.filter(price__gte=price_min)
        if price_max is not None:
            products = products.filter(price__lte=price_max)

        # ۴.۸. اعمال ترتیب نمایش (جدیدترین/ارزان‌ترین/گران‌ترین/پرفروش‌ترین/پربازدیدترین/بیشترین امتیاز)
        sort = request.GET.get('sort', 'newest')
        if sort not in PRODUCT_SORT_VALUES:
            sort = 'newest'
        products = _apply_sort(products, sort)

        # ۵. صفحه‌بندی نتایج (با windowing برای جلوگیری از شکستن نوار صفحه‌بندی روی کاتالوگ بزرگ)
        paginator = Paginator(products.distinct(), PRODUCTS_PER_PAGE)
        page_number = request.GET.get('page', 1)
        page_obj = paginator.get_page(page_number)
        elided_page_range = list(page_obj.paginator.get_elided_page_range(page_obj.number, on_each_side=1, on_ends=1))

        # querystring فعلی بدون page، برای استفاده در لینک‌های صفحه‌بندی (تمام فیلترهای فعال را حفظ می‌کند)
        querydict = request.GET.copy()
        querydict.pop('page', None)
        base_qs = querydict.urlencode()

        # داده‌ی فیلترهای سایدبار
        price_bounds = Product.objects.filter(is_active=True).aggregate(min_price=Min('price'), max_price=Max('price'))
        available_colors = (
            ProductColor.objects.filter(product__is_active=True)
            .values('name', 'hex_code').distinct().order_by('name')
        )
        available_brands = (
            Brand.objects.filter(is_active=True, products__is_active=True).distinct().order_by('name')
        )

        context = {
            'products': page_obj,
            'page_obj': page_obj,
            'elided_page_range': elided_page_range,
            'base_qs': base_qs,
            'categories': categories,
            'current_category': category_slug,
            'current_brands': brand_slugs,
            'current_color': color,
            'price_min': price_min,
            'price_max': price_max,
            'price_bounds': price_bounds,
            'available_colors': available_colors,
            'available_brands': available_brands,
            'search_query': search_query,
            'current_sort': sort,
            'sort_options': PRODUCT_SORT_OPTIONS,
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
        return Product.objects.filter(is_active=True).select_related('category', 'brand')


    def get(self, request, *args, **kwargs):
        response = super().get(request, *args, **kwargs)
        if request.user.is_authenticated:
            RecentlyViewed.track(user=request.user, product=self.object)
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # واکشی مشخصات فنی (EAV) مربوط به همین محصول بهینه‌سازی شده با select_related
        context['features'] = self.object.features.select_related('feature').all()

        # گالری تصاویر: تصویر اصلی همیشه اول است، بعد تصاویر گالری به ترتیب
        context['gallery_images'] = list(self.object.gallery_images.all())

        # رنگ‌بندی محصول (اولین رنگِ پیش‌فرض یا اولین رنگِ لیست، همانی که در سواچ رادیویی هم به‌طور پیش‌فرض تیک می‌خورد)
        colors = list(self.object.colors.all())
        context['colors'] = colors
        context['default_color'] = next((c for c in colors if c.is_default), colors[0] if colors else None)

        # شمارش محصولات مشابه برای لینک‌های «مشاهده محصولات دیگر» برند/دسته‌بندی
        context['brand_count'] = 0
        if self.object.brand_id:
            context['brand_count'] = Product.objects.filter(
                brand_id=self.object.brand_id, is_active=True
            ).exclude(id=self.object.id).count()

        context['category_count'] = 0
        if self.object.category_id:
            context['category_count'] = Product.objects.filter(
                category_id=self.object.category_id, is_active=True
            ).exclude(id=self.object.id).count()

        context['is_comparing'] = self.object.id in self.request.session.get('compare_ids', [])

        published_reviews = Review.objects.filter(product=self.object, parent__isnull=True, status='published')

        sort = self.request.GET.get('sort', 'newest')
        order_by = REVIEW_SORT_OPTIONS.get(sort, REVIEW_SORT_OPTIONS['newest'])
        context['reviews'] = published_reviews.order_by(*order_by).select_related('user').prefetch_related(
            'points', 'images', 'replies__user', 'replies__points', 'replies__images', 'replies__replies__user',
        )
        context['reviews_sort'] = sort

        counts_map = {row['rating']: row['count'] for row in published_reviews.values('rating').annotate(count=Count('id'))}
        total = sum(counts_map.values())
        context['rating_summary'] = {
            'average': published_reviews.aggregate(avg=Avg('rating'))['avg'] or 0,
            'count': total,
            'histogram': [
                {
                    'star': star,
                    'count': counts_map.get(star, 0),
                    'percent': round((counts_map.get(star, 0) / total) * 100) if total else 0,
                }
                for star in range(5, 0, -1)
            ],
        }

        context['can_write_review'] = self.request.user.is_authenticated
        context['user_review'] = None
        if self.request.user.is_authenticated:
            context['user_review'] = Review.objects.filter(
                product=self.object, user=self.request.user, parent__isnull=True
            ).first()

        return context