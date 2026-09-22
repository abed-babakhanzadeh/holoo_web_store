from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.core.validators import validate_email
from django.db.models import Avg, Count, Min, Max, Sum, Q
from django.shortcuts import get_object_or_404, render
from django.views import View
from . import home_cache
from .blog_posts import latest_posts as _latest_posts
from . import deals
from .deals import flash_deals_filter
from .models import Product, Category, Brand, ProductColor, ProductFeatureValue, StockAlert, SiteSettings, Story, HomeBanner, NewsletterSubscriber
from .ordering import stock_first
from .pricing import GUEST_HIDE_PRICE, annotate_effective_price, guest_pricing_config
from django.views.generic import DetailView
from recently_viewed.models import RecentlyViewed
from reviews.constants import DEFAULT_REVIEW_SORT, review_order_by
from reviews.models import Review
from services.text import normalize_persian
from accounts.models import normalize_phone_number

PRODUCTS_PER_PAGE = 12

# ترتیب‌های مجاز فروشگاه؛ کلید = مقدار پارامتر sort در URL، مقدار = برچسب نمایشی
PRODUCT_SORT_OPTIONS = (
    ('newest', 'جدیدترین'),
    ('price_asc', 'ارزان‌ترین'),
    ('price_desc', 'گران‌ترین'),
    ('best_selling', 'پرفروش‌ترین'),
    ('most_viewed', 'پربازدیدترین'),
    ('top_rated', 'بیشترین امتیاز'),
    ('discount', 'بیشترین تخفیف'),
)
PRODUCT_SORT_VALUES = {key for key, _ in PRODUCT_SORT_OPTIONS}
# سفارش‌هایی که «فروش واقعی‌شده» حساب می‌شوند (لغوشده و در انتظار پرداخت حساب نمی‌شوند)
SOLD_ORDER_STATUSES = ('registered', 'processing', 'shipped', 'delivered')


def _apply_sort(products, sort):
    """
    اعمال ترتیب روی کوئری‌ست محصولات؛ برای گزینه‌های آماری، annotate لازم انجام می‌شود.
    در همه‌ی حالت‌ها stock_first پیش‌فرض است: محصولات ناموجود همیشه بعد از موجودها می‌آیند
    (ترتیب دوم/آماری داخل هر گروه اعمال می‌شود)، یک نقطه‌ی مشترک برای این قاعده در کل سایت.
    """
    if sort == 'price_asc':
        # effective_price از annotate_effective_price می‌آید (سطح قیمت/روش پرداختِ همین کاربر/مهمان، نه
        # همیشه price خام)؛ صدازننده باید از قبل queryset را annotate کرده باشد (ProductListView.get)
        return stock_first(products, 'effective_price', 'id')
    if sort == 'price_desc':
        return stock_first(products, '-effective_price', 'id')
    if sort == 'best_selling':
        return stock_first(
            products.annotate(
                sold_count=Sum('order_items__quantity', filter=Q(order_items__order__status__in=SOLD_ORDER_STATUSES))
            ), '-sold_count', '-created_at', 'id'
        )
    if sort == 'most_viewed':
        return stock_first(
            products.annotate(view_count=Count('recently_viewed_by', distinct=True)),
            '-view_count', '-created_at', 'id'
        )
    if sort == 'top_rated':
        return stock_first(
            products.annotate(
                avg_rating=Avg('reviews__rating', filter=Q(reviews__status='published', reviews__parent__isnull=True))
            ), '-avg_rating', '-created_at', 'id'
        )
    return stock_first(products, '-created_at', 'id')  # 'newest' (پیش‌فرض)


def _flash_deals(category_ids=None):
    """
    محصولات دارای تخفیف «شگفت‌انگیز» فعال در همین لحظه (+ زودترین پایان بین این تخفیف‌ها، برای تایمر
    شمارش معکوس باکس)، اختیاری محدود به یک دسته + زیردسته‌هایش. اینکه چه چیزی «شگفت‌انگیز» است را اپ
    promotions مشخص می‌کند (products/deals.py)؛ اینجا فقط لیست را می‌سازیم.
    """
    deals_filter, nearest_ends_at = flash_deals_filter(category_ids=category_ids)
    if deals_filter is None:
        return Product.visible.none(), None
    products = Product.visible.filter(deals_filter).prefetch_related('colors', 'gallery_images')
    if category_ids is not None:
        products = products.filter(category_id__in=category_ids)
    return stock_first(products, '-created_at')[:10], nearest_ends_at


def _visible_products_by_ids(ids):
    """
    محصولات موجود در ids را با prefetch استاندارد کارت محصول برمی‌گرداند، با همان ترتیب
    ورودی (نه ترتیب پیش‌فرض دیتابیس) - قیمت/موجودی/تخفیف همیشه لحظه‌ای خوانده می‌شود، فقط
    خودِ انتخاب (کدام id ها) ممکن است از home_cache آمده باشد؛ نگاه کنید آن فایل.
    """
    if not ids:
        return []
    products = Product.visible.filter(id__in=ids).select_related('category').prefetch_related(
        'colors', 'gallery_images'
    )
    by_id = {p.id: p for p in products}
    return [by_id[i] for i in ids if i in by_id]


def _newest_product_ids(limit=8):
    return list(stock_first(Product.visible, '-created_at').values_list('id', flat=True)[:limit])


def _best_selling_product_ids(limit=12):
    return list(_apply_sort(Product.visible, 'best_selling').values_list('id', flat=True)[:limit])


def _most_viewed_product_ids(limit=12):
    return list(_apply_sort(Product.visible, 'most_viewed').values_list('id', flat=True)[:limit])


def _compute_top_category_ids(limit=8):
    """
    دسته‌های ریشه‌ی فعال، مرتب بر اساس مجموع فروش واقعی‌شده‌ی محصولات خودشان + همه‌ی
    زیردسته‌هایشان (چون محصولات معمولاً به زیردسته وصل‌اند، نه مستقیم ریشه؛ نگاه کنید
    Product.category). همیشه تا `limit` دسته برمی‌گرداند، حتی اگر فروشی نداشته باشند
    (دسته‌های بی‌فروش فقط رتبه‌شان پایین‌تر است، حذف نمی‌شوند).
    """
    roots = list(Category.objects.filter(is_active=True, parent__isnull=True).only('id', 'name'))
    scored = []
    for cat in roots:
        descendant_ids = cat.get_descendant_ids(include_self=True)
        sold_count = Product.objects.filter(category_id__in=descendant_ids).aggregate(
            total=Sum('order_items__quantity', filter=Q(order_items__order__status__in=SOLD_ORDER_STATUSES))
        )['total'] or 0
        scored.append((sold_count, cat.name, cat.id))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [cat_id for _, _, cat_id in scored[:limit]]


def _top_categories(limit=8):
    ids = home_cache.get_ids(home_cache.TOP_CATEGORY_IDS, lambda: _compute_top_category_ids(limit))
    if not ids:
        return []
    cats = Category.objects.in_bulk(ids)
    return [cats[i] for i in ids if i in cats]


def _compute_popular_brand_ids(limit=10):
    visible_product = Q(products__is_active=True, products__price__gt=0)
    qs = Brand.objects.filter(is_active=True).filter(visible_product).annotate(
        product_count=Count('products', distinct=True, filter=visible_product)
    ).filter(product_count__gt=0).order_by('-product_count', 'name')[:limit]
    return list(qs.values_list('id', flat=True))


def _popular_brands(limit=10):
    """ برندهای دارای محصول قابل‌نمایش، بر اساس تعداد محصول فعال مرتب‌شده (پرمحصول‌ترین بالاتر) """
    ids = home_cache.get_ids(home_cache.POPULAR_BRAND_IDS, lambda: _compute_popular_brand_ids(limit))
    if not ids:
        return []
    # product_count دوباره (زنده) محاسبه می‌شود؛ فقط انتخاب برندها از کش می‌آید نه تعدادشان
    visible_product = Q(products__is_active=True, products__price__gt=0)
    brands = Brand.objects.filter(id__in=ids).annotate(
        product_count=Count('products', distinct=True, filter=visible_product)
    )
    by_id = {b.id: b for b in brands}
    return [by_id[i] for i in ids if i in by_id]


def _compute_stories_data():
    """
    فهرست سبک (دیکشنری، نه instance) استوری‌های فعال، برای کش‌شدن امن (بدون درگیری
    serialize فایل‌فیلد). اگر سوییچ سراسری خاموش باشد، حتی یک کوئری هم به Story زده
    نمی‌شود.
    """
    if not SiteSettings.cached().show_stories:
        return []
    stories = Story.visible.select_related('link_product')
    return [
        {
            'id': s.id,
            'title': s.title,
            'type': s.story_type,
            'cover_url': s.cover_image.url,
            'media_url': s.media_url,
            'duration': s.duration_ms,
            'link': s.target_url,
        }
        for s in stories
    ]


def _stories_data():
    return home_cache.get_ids(home_cache.STORIES, _compute_stories_data)


def _home_banners():
    """ {slot: HomeBanner} فقط برای جایگاه‌های فعال؛ کوئری همیشه حداکثر ۴ ردیف است، نیازی به کش نیست """
    banners = HomeBanner.objects.filter(is_active=True).select_related('link_product', 'link_category')
    return {b.slot: b for b in banners}


class HomeView(View):
    """ ویوی صفحه اصلی (ویترین) فروشگاه """

    def get(self, request, *args, **kwargs):
        newest_ids = home_cache.get_ids(home_cache.NEWEST_IDS, lambda: _newest_product_ids(8))
        best_selling_ids = home_cache.get_ids(home_cache.BEST_SELLING_IDS, lambda: _best_selling_product_ids(12))
        most_viewed_ids = home_cache.get_ids(home_cache.MOST_VIEWED_IDS, lambda: _most_viewed_product_ids(12))

        products = _visible_products_by_ids(newest_ids)
        best_selling_products = _visible_products_by_ids(best_selling_ids)
        most_viewed_products = _visible_products_by_ids(most_viewed_ids)
        top_categories = _top_categories(limit=8)
        popular_brands = _popular_brands(limit=10)
        latest_posts = _latest_posts(limit=6)
        flash_deal_products, deal_ends_at = _flash_deals()
        stories = _stories_data()
        banners = _home_banners()
        context = {
            'products': products,
            'top_categories': top_categories,
            'flash_deal_products': flash_deal_products,
            'deal_ends_at': deal_ends_at,
            'most_viewed_products': most_viewed_products,
            'best_selling_products': best_selling_products,
            'popular_brands': popular_brands,
            'latest_posts': latest_posts,
            'stories': stories,
            'banners': banners,
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
        products = Product.visible.select_related('category').prefetch_related('colors', 'gallery_images').order_by('-created_at', 'id')

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

        # فقط فیلترهایی که با JOIN روی یک رابطه‌ی چندتایی (رنگ/مشخصات فنی) اعمال می‌شوند، ممکن
        # است یک محصول را چندبار در نتیجه تکرار کنند؛ distinct() فقط وقتی لازم است که واقعاً
        # یکی از این‌ها فعال باشد، نه برای هر بازدید ساده‌ی صفحه‌بندی (که COUNT/SELECT را روی
        # کل کاتالوگ بی‌جهت گران‌تر می‌کرد)
        needs_distinct = False

        # ۴.۶. اعمال فیلتر رنگ
        color = request.GET.get('color')
        if color:
            products = products.filter(colors__name=color)
            needs_distinct = True

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
                needs_distinct = True

        # ۴.۷. تعیین ترتیب نمایش (جدیدترین/ارزان‌ترین/گران‌ترین/پرفروش‌ترین/پربازدیدترین/بیشترین امتیاز/بیشترین تخفیف)
        sort = request.GET.get('sort', 'newest')
        if sort not in PRODUCT_SORT_VALUES:
            sort = 'newest'

        # ۴.۸. فیلتر بازه‌ی قیمت + مرتب‌سازی «ارزان‌ترین/گران‌ترین»: هر دو روی effective_price کار می‌کنند
        # (سطح قیمت/روش پرداختِ همین کاربر یا مهمان، نه همیشه Product.price خام - قبلاً برای کاربر نقدی/ویژه
        # فیلتر و مرتب‌سازی با قیمتی که روی کارت می‌دید نمی‌خواند). برای مهمانِ حالت «مخفی‌سازی قیمت»، چون او
        # اصلاً قیمتی نمی‌بیند، price_min/price_max/sort=price_* ارسالی در URL کاملاً نادیده گرفته می‌شوند؛
        # وگرنه با جستجوی دودویی روی همین پارامترها می‌شد بازه‌ی قیمت واقعی کالاها را حدس زد.
        price_filter_blocked = not request.user.is_authenticated and guest_pricing_config().mode == GUEST_HIDE_PRICE
        if price_filter_blocked and sort in ('price_asc', 'price_desc'):
            sort = 'newest'
        price_min = None if price_filter_blocked else _parse_price(request.GET.get('price_min'))
        price_max = None if price_filter_blocked else _parse_price(request.GET.get('price_max'))

        # annotate فقط وقتی واقعاً لازم است (فیلتر یا مرتب‌سازی قیمتی فعال باشد)؛ بدون کوئری اضافه، چون
        # فقط یک ستون محاسبه‌شده به همان SELECT موجود اضافه می‌شود، نه یک رفت‌وبرگشتِ جدا به دیتابیس
        if price_min is not None or price_max is not None or sort in ('price_asc', 'price_desc'):
            products = annotate_effective_price(products, request.user)
            if price_min is not None:
                products = products.filter(effective_price__gte=price_min)
            if price_max is not None:
                products = products.filter(effective_price__lte=price_max)

        # ۴.۹. فیلتر «فقط کالاهای دارای تخفیف» و ترتیب «بیشترین تخفیف»: تخفیف از همان موتور قیمتِ کارت/سبد/فاکتور برای
        # *همین کاربر* حساب می‌شود (نگاه کنید promotions/catalog.py)؛ بدون منطق قیمتی دوم و با یک کوئریِ اضافه
        discount_only = request.GET.get('discount') == '1'
        discount_available = deals.discount_catalog_available()
        if not discount_available:                      # اپ تخفیف‌ها نصب/ثبت نیست: گزینه‌ها بی‌اثر و پنهان‌اند
            discount_only = False
            if sort == 'discount':
                sort = 'newest'
        catalog = None
        if discount_only or sort == 'discount':
            catalog = deals.discount_catalog(request.user, products)
        if discount_only:
            products = catalog.filter_queryset(products)

        if needs_distinct:
            products = products.distinct()

        if sort == 'discount':
            # ترتیب در پایتون از روی شناسه‌ها ساخته می‌شود؛ فقط ۱۲ کالای صفحه‌ی جاری با همه‌ی join/prefetch‌ها بار می‌شود
            ordered_ids = catalog.ordered_ids(products)
            paginator = Paginator(ordered_ids, PRODUCTS_PER_PAGE)
            page_obj = paginator.get_page(request.GET.get('page', 1))
            by_id = {p.pk: p for p in products.filter(pk__in=list(page_obj.object_list)).order_by()}
            page_obj.object_list = [by_id[pk] for pk in page_obj.object_list if pk in by_id]
        else:
            products = _apply_sort(products, sort)
            # ۵. صفحه‌بندی نتایج (با windowing برای جلوگیری از شکستن نوار صفحه‌بندی روی کاتالوگ بزرگ)
            paginator = Paginator(products, PRODUCTS_PER_PAGE)
            page_obj = paginator.get_page(request.GET.get('page', 1))
        elided_page_range = list(page_obj.paginator.get_elided_page_range(page_obj.number, on_each_side=1, on_ends=1))

        # querystring فعلی بدون page، برای استفاده در لینک‌های صفحه‌بندی (تمام فیلترهای فعال را حفظ می‌کند)
        querydict = request.GET.copy()
        querydict.pop('page', None)
        base_qs = querydict.urlencode()

        # داده‌ی فیلترهای سایدبار
        # مهمانِ حالت «مخفی‌سازی قیمت» کران‌های قیمت هم نمی‌بیند (پنل فیلترِ قیمت برایش اصلاً رندر نمی‌شود؛
        # نگاه کنید filter_panel.html)؛ مقدار پوچ هم یک کوئری اضافه‌ی بی‌مصرف را حذف می‌کند
        if price_filter_blocked:
            price_bounds = {'min_price': None, 'max_price': None}
        else:
            price_bounds = annotate_effective_price(Product.visible, request.user).aggregate(
                min_price=Min('effective_price'), max_price=Max('effective_price')
            )
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
            'discount_only': discount_only,
            'discount_available': discount_available,
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
        # فقط محصولات فعال و دارای قیمت فروش اجازه نمایش دارند.
        # تخفیف‌های خودکار از شاخص درون‌حافظه‌ی اپ promotions می‌آیند؛ prefetch جدایی لازم نیست
        # category__parent هم اینجا select_related می‌شود چون بردکرامب یک سطح بالاتر
        # می‌رود؛ بدونش product.category.parent یک کوئری جدا می‌زد.
        return Product.visible.select_related('category__parent', 'brand').prefetch_related(
            'colors', 'gallery_images', 'features__feature',
        )


    def get(self, request, *args, **kwargs):
        response = super().get(request, *args, **kwargs)
        if request.user.is_authenticated:
            RecentlyViewed.track(user=request.user, product=self.object)
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # واکشی مشخصات فنی (EAV) مربوط به همین محصول: از prefetch_related('features__feature')
        # بالای get_queryset استفاده می‌شود؛ select_related('feature').all() اینجا یک کوئری‌ست
        # کاملاً تازه می‌ساخت که آن prefetch را دور می‌زد و یک کوئری بی‌فایده اضافه می‌کرد.
        context['features'] = self.object.features.all()

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

        sort = self.request.GET.get('sort', DEFAULT_REVIEW_SORT)
        # زنجیره‌ی prefetch تا عمق سوم پاسخ‌ها ادامه دارد؛ هر سطحی که جا بیفتد، قالبِ
        # بازگشتیِ نظرات برای تک‌تک گره‌های آن سطح یک کوئری جدا می‌زند
        context['reviews'] = published_reviews.order_by(*review_order_by(sort)).select_related('user').prefetch_related(
            'points', 'images',
            'replies__user', 'replies__points', 'replies__images',
            'replies__replies__user', 'replies__replies__points', 'replies__replies__images',
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
        context['stock_alert'] = None
        if self.request.user.is_authenticated:
            context['user_review'] = Review.objects.filter(
                product=self.object, user=self.request.user, parent__isnull=True
            ).first()
            if self.object.stock <= 0:
                context['stock_alert'] = StockAlert.objects.filter(
                    product=self.object, user=self.request.user, status=StockAlert.STATUS_PENDING,
                ).first()

        return context


class StockAlertView(LoginRequiredMixin, View):
    """ ثبت/به‌روزرسانی/لغو درخواست «اطلاع بده وقتی موجود شد» برای یک محصول ناموجود """

    def post(self, request, product_id, *args, **kwargs):
        product = get_object_or_404(Product, id=product_id)

        if request.POST.get('action') == 'cancel':
            StockAlert.objects.filter(product=product, user=request.user).delete()
            return render(request, 'products/partials/stock_alert_box.html', {
                'product': product, 'stock_alert': None, 'just_action': 'cancelled',
            })

        # محصول در همین فاصله موجود شده؛ دیگر درخواستی معنا ندارد (باکس معمولی خرید نمایش داده شود)
        if product.stock > 0:
            return render(request, 'products/partials/stock_alert_box.html', {'product': product, 'stock_alert': None})

        # کاربر می‌تواند یکی از دو کانال (پیامک/ایمیل) یا هر دو را انتخاب کند
        selected = [c for c in request.POST.getlist('channel') if c in (StockAlert.CHANNEL_SMS, StockAlert.CHANNEL_EMAIL)]
        typed_email = request.POST.get('email', '').strip()

        error = None
        alert_email = ''
        if not selected:
            error = 'حداقل یکی از روش‌های اطلاع‌رسانی را انتخاب کنید.'
        elif StockAlert.CHANNEL_EMAIL in selected:
            email = typed_email or request.user.email or ''
            if not email:
                error = 'برای اطلاع‌رسانی ایمیلی، وارد کردن ایمیل لازم است.'
            else:
                try:
                    validate_email(email)
                except ValidationError:
                    error = 'ایمیل واردشده معتبر نیست.'
                else:
                    if not request.user.email:
                        # کاربر اصلاً ایمیلی در پروفایل نداشت؛ همین یکی پروفایلش را هم پر می‌کند
                        # تا دفعه‌ی بعد دوباره از او پرسیده نشود
                        request.user.email = email
                        request.user.save(update_fields=['email'])
                    elif email != request.user.email:
                        # کاربر از قبل ایمیل پروفایل داشت و اینجا ایمیل دیگری داد: فقط برای همین
                        # درخواست استفاده می‌شود، ایمیل پروفایلش دست‌نخورده می‌ماند
                        alert_email = email

        if error:
            return render(request, 'products/partials/stock_alert_box.html', {
                'product': product, 'stock_alert': None, 'error': error,
                'selected_channels': selected, 'typed_email': typed_email,
            })

        channel = StockAlert.CHANNEL_BOTH if len(selected) == 2 else selected[0]
        stock_alert, _ = StockAlert.objects.update_or_create(
            product=product, user=request.user,
            defaults={
                'channel': channel, 'email': alert_email,
                'status': StockAlert.STATUS_PENDING, 'notified_at': None,
            },
        )
        return render(request, 'products/partials/stock_alert_box.html', {
            'product': product, 'stock_alert': stock_alert, 'just_action': 'subscribed',
        })


class NewsletterSubscribeView(View):
    """ ثبت شماره موبایل در خبرنامه (فوتر)؛ بدون نیاز به لاگین """

    def post(self, request, *args, **kwargs):
        raw_phone = request.POST.get('phone_number', '')
        error = None
        success = False
        try:
            phone = normalize_phone_number(raw_phone)
        except ValueError:
            error = 'شماره موبایل واردشده معتبر نیست.'
        else:
            NewsletterSubscriber.objects.get_or_create(phone_number=phone)
            success = True
        return render(request, 'products/partials/newsletter_form.html', {
            'success': success, 'error': error, 'phone_number': raw_phone,
        })


def _distinct_order_count_annotation(products, descendant_ids):
    """ محصولات یک دسته، مرتب‌شده بر اساس تعداد سفارش‌های متمایزی که در آن‌ها دیده شده‌اند («پرتکرارها») """
    return stock_first(
        products.filter(category_id__in=descendant_ids).annotate(
            order_count=Count('order_items__order', distinct=True, filter=Q(order_items__order__status__in=SOLD_ORDER_STATUSES))
        ), '-order_count', '-created_at', 'id'
    )


class CategoryDetailView(View):
    """ صفحه‌ی فرود اختصاصی یک دسته‌ی سطح‌بالا (نه زیردسته)؛ برای زیردسته یا دسته‌ی غیرفعال ۴۰۴ می‌دهد """

    def get(self, request, slug, *args, **kwargs):
        category = get_object_or_404(Category, slug=slug, parent__isnull=True, is_active=True)
        descendant_ids = category.get_descendant_ids()

        context = {
            'category': category,
            'subcategories': category.children.filter(is_active=True),
        }

        if category.show_amazing_deals:
            context['flash_deal_products'], context['deal_ends_at'] = _flash_deals(descendant_ids)

        if category.show_best_sellers:
            context['best_seller_products'] = _apply_sort(
                Product.visible.filter(category_id__in=descendant_ids), 'best_selling'
            )[:10]

        if category.show_frequent:
            context['frequent_products'] = _distinct_order_count_annotation(
                Product.visible.prefetch_related('colors', 'gallery_images'), descendant_ids
            )[:10]

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
