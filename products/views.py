from django.core.paginator import Paginator
from django.db.models import Avg, Count, Min, Max, Sum, Q
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views import View
from .models import Product, Category, Brand, ProductColor, ProductFeatureValue
from django.views.generic import DetailView
from recently_viewed.models import RecentlyViewed
from reviews.models import Review
from services.text import normalize_persian

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
        products = Product.visible.select_related('category').order_by('-created_at')[:8]
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
        products = Product.visible.select_related('category').order_by('-created_at', 'id')

        # ۲. دریافت دسته‌بندی‌های اصلی (آن‌هایی که پدر ندارند) برای سایدبار
        categories = Category.objects.filter(is_active=True, parent__isnull=True).prefetch_related('children')

        # ۳. اعمال فیلتر جستجوی متنی (تایپ زنده)
        search_query = request.GET.get('q', '').strip()
        if search_query:
            products = products.filter(name_normalized__icontains=normalize_persian(search_query))

        # ۴. اعمال فیلتر دسته‌بندی (خودش + همه‌ی زیردسته‌ها در هر عمقی، نه فقط یک سطح)
        category_slug = request.GET.get('category')
        selected_category = None
        subcategories = None
        if category_slug:
            selected_category = Category.objects.filter(slug=category_slug).first()
            if selected_category:
                products = products.filter(category_id__in=selected_category.get_descendant_ids())
                subcategories = selected_category.children.filter(is_active=True)
            else:
                products = products.none()

        # کوئری‌ست مبنا برای ساخت فهرست فیلترهای پویای مشخصات فنی: فقط با q + دسته فیلتر شده،
        # نه با بقیه‌ی فیلترهای فعال (هم‌راستا با available_colors/available_brands که هم «کل
        # کاتالوگ دیده‌شده» را نشان می‌دهند، نه narrowing تدریجی)
        category_scoped_products = products

        # ۴.۵. اعمال فیلتر برند (چندتایی؛ سازگار با لینک تک‌برندی «محصولات دیگر این برند» در صفحه محصول)
        brand_slugs = request.GET.getlist('brand')
        if brand_slugs:
            products = products.filter(brand__slug__in=brand_slugs)

        # ۴.۶. اعمال فیلتر رنگ
        color = request.GET.get('color')
        if color:
            products = products.filter(colors__name=color)

        # ۴.۶.۱. اعمال فیلتر ارسال رایگان
        free_shipping = request.GET.get('free_shipping') == '1'
        if free_shipping:
            products = products.filter(free_shipping=True)

        # ۴.۶.۲. اعمال فیلتر کالاهای موجود
        in_stock = request.GET.get('in_stock') == '1'
        if in_stock:
            products = products.filter(stock__gt=0)

        # ۴.۶.۳. اعمال فیلترهای پویای مشخصات فنی (attr_<feature_id>=value، چندمقداری)
        # QueryDict کمکی برای «کلیدهای با این پیشوند» ندارد، پس دستی حلقه می‌زنیم
        for key in request.GET.keys():
            if not key.startswith('attr_'):
                continue
            feature_id_str = key[len('attr_'):]
            if not feature_id_str.isdigit():
                continue
            values = request.GET.getlist(key)
            if values:
                # هر ویژگی یک .filter() جداگانه روی رابطه‌ی معکوس features؛ چون هرکدام JOIN
                # جدا می‌سازد، میان ویژگی‌های مختلف AND می‌شود (نه OR)؛ مقادیر مختلف همان
                # ویژگی با __in خودش OR می‌شوند
                products = products.filter(features__feature_id=int(feature_id_str), features__value__in=values)

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
        price_bounds = Product.visible.aggregate(min_price=Min('price'), max_price=Max('price'))
        available_colors = (
            ProductColor.objects.filter(product__is_active=True, product__price__gt=0)
            .values('name', 'hex_code').distinct().order_by('name')
        )
        available_brands = (
            Brand.objects.filter(is_active=True, products__is_active=True, products__price__gt=0).distinct().order_by('name')
        )

        # فهرست فیلترهای پویای مشخصات فنی، فقط وقتی روی یک دسته فیلتر شده باشیم (مشخصات فنی
        # خارج از یک دسته‌ی مشخص معنای فیلترکردنی ندارند)
        feature_facets = []
        if selected_category:
            rows = (
                ProductFeatureValue.objects
                .filter(product__in=category_scoped_products)
                .values('feature_id', 'feature__name', 'value')
                .distinct().order_by('feature__name', 'value')
            )
            grouped = {}
            for row in rows:
                grouped.setdefault(row['feature_id'], {'name': row['feature__name'], 'values': []})
                grouped[row['feature_id']]['values'].append(row['value'])
            feature_facets = [{'feature_id': fid, **data} for fid, data in grouped.items()]

        context = {
            'products': page_obj,
            'page_obj': page_obj,
            'elided_page_range': elided_page_range,
            'base_qs': base_qs,
            'categories': categories,
            'selected_category': selected_category,
            'subcategories': subcategories,
            'current_category': category_slug,
            'current_brands': brand_slugs,
            'current_color': color,
            'free_shipping': free_shipping,
            'in_stock': in_stock,
            'feature_facets': feature_facets,
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
            products = Product.visible.filter(
                name_normalized__icontains=normalize_persian(query)
            ).select_related('category')[:6]
        return render(request, 'products/partials/live_search_results.html', {'query': query, 'products': products})


class ProductDetailView(DetailView):
    """ ویوی نمایش جزئیات کامل یک محصول و مشخصات فنی آن """
    model = Product
    template_name = 'products/product_detail.html'
    context_object_name = 'product'
    
    def get_queryset(self):
        # فقط محصولات فعال و دارای قیمت فروش اجازه نمایش دارند
        return Product.visible.select_related('category', 'brand')


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
            context['brand_count'] = Product.visible.filter(
                brand_id=self.object.brand_id
            ).exclude(id=self.object.id).count()

        context['category_count'] = 0
        if self.object.category_id:
            context['category_count'] = Product.visible.filter(
                category_id=self.object.category_id
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


def _distinct_order_count_annotation(products, descendant_ids):
    """ محصولات یک دسته، مرتب‌شده بر اساس تعداد سفارش‌های متمایزی که در آن‌ها دیده شده‌اند («پرتکرارها») """
    return products.filter(category_id__in=descendant_ids).annotate(
        order_count=Count('order_items__order', distinct=True, filter=Q(order_items__order__status__in=SOLD_ORDER_STATUSES))
    ).order_by('-order_count', '-created_at', 'id')


class CategoryDetailView(View):
    """ صفحه‌ی فرود اختصاصی یک دسته‌ی سطح‌بالا (نه زیردسته)؛ برای زیردسته یا دسته‌ی غیرفعال ۴۰۴ می‌دهد """

    def get(self, request, slug, *args, **kwargs):
        category = get_object_or_404(Category, slug=slug, parent__isnull=True, is_active=True)
        descendant_ids = category.get_descendant_ids()
        now = timezone.now()

        context = {
            'category': category,
            'subcategories': category.children.filter(is_active=True),
        }

        if category.show_amazing_deals:
            context['flash_deal_products'] = Product.visible.filter(
                category_id__in=descendant_ids,
                discounts__is_active=True, discounts__starts_at__lte=now, discounts__ends_at__gte=now,
            ).distinct()[:10]

        if category.show_best_sellers:
            context['best_seller_products'] = _apply_sort(
                Product.visible.filter(category_id__in=descendant_ids), 'best_selling'
            )[:10]

        if category.show_frequent:
            context['frequent_products'] = _distinct_order_count_annotation(Product.visible, descendant_ids)[:10]

        if category.show_suggested_categories:
            context['suggested_categories'] = category.suggested_categories.filter(is_active=True)

        if category.show_banners:
            context['banners'] = category.banners.select_related('link_product')[:5]

        if category.show_blog_posts:
            from blog.models import Post  # ایمپورت محلی، هم‌راستا با الگوی products/context_processors.py
            context['related_posts'] = Post.visible.filter(
                category__in=category.related_blog_categories.all()
            ).select_related('category')[:8]

        return render(request, 'products/category_detail.html', context)