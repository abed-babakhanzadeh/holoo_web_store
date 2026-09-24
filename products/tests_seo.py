"""تست‌های SEO: Phase B2 (robots.txt / sitemap.xml) و Phase D (canonical URL کاتالوگ)."""

import re
import xml.etree.ElementTree as ET

from django.test import TestCase
from django.urls import reverse

from blog.models import Post
from products.models import Brand, Category, Product

SITEMAP_NS = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
CANONICAL_RE = re.compile(r'<link rel="canonical" href="([^"]*)"')
ROBOTS_RE = re.compile(r'<meta name="robots" content="([^"]*)"')


class RobotsTxtTests(TestCase):
    DISALLOWED_PREFIXES = (
        '/admin/', '/accounts/', '/cart/', '/orders/', '/payments/',
        '/wishlist/', '/compare/', '/my-discounts/', '/recently-viewed/',
        '/reviews/', '/locations/', '/ckeditor5/',
    )

    def test_returns_200_as_plain_text(self):
        response = self.client.get('/robots.txt')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/plain; charset=utf-8')

    def test_disallows_transactional_and_private_paths(self):
        content = self.client.get('/robots.txt').content.decode()
        for prefix in self.DISALLOWED_PREFIXES:
            self.assertIn(f'Disallow: {prefix}', content)

    def test_login_page_explicitly_allowed(self):
        # Allow با مسیر طولانی‌تر باید همچنان صریح در فایل باشد؛ صرفِ نبودِ Disallow کافی نیست
        content = self.client.get('/robots.txt').content.decode()
        self.assertIn('Allow: /accounts/login/', content)

    def test_search_query_and_static_media_not_disallowed(self):
        content = self.client.get('/robots.txt').content.decode()
        self.assertNotIn('Disallow: /shop/?q', content)
        self.assertNotIn('Disallow: /static/', content)
        self.assertNotIn('Disallow: /media/', content)

    def test_declares_dynamic_sitemap_url(self):
        response = self.client.get('/robots.txt')
        content = response.content.decode()
        self.assertIn('Sitemap: http', content)
        self.assertIn('/sitemap.xml', content)
        # دامنه باید از خودِ request بیاید، نه یک مقدار هاردکد
        self.assertIn(response.wsgi_request.get_host(), content)


class SitemapXmlTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.top_category = Category.objects.create(name='دسته فعال سایت‌مپ', slug='sitemap-active-cat')
        cls.sub_category = Category.objects.create(
            name='زیردسته سایت‌مپ', slug='sitemap-sub-cat', parent=cls.top_category,
        )
        cls.inactive_category = Category.objects.create(
            name='دسته غیرفعال سایت‌مپ', slug='sitemap-inactive-cat', is_active=False,
        )

        cls.product = Product.objects.create(
            name='محصول سایت‌مپ', slug='sitemap-product', erp_code='ERP-SITEMAP-1',
            category=cls.top_category, price=50000, stock=5,
        )
        cls.inactive_product = Product.objects.create(
            name='محصول غیرفعال سایت‌مپ', slug='sitemap-inactive-product', erp_code='ERP-SITEMAP-2',
            category=cls.top_category, price=50000, stock=5, is_active=False,
        )

        cls.published_post = Post.objects.create(title='مقاله منتشرشده سایت‌مپ', slug='sitemap-published-post', status='published')
        cls.draft_post = Post.objects.create(title='مقاله پیش‌نویس سایت‌مپ', slug='sitemap-draft-post', status='draft')

    def _fetch_locs(self):
        response = self.client.get('/sitemap.xml')
        self.assertEqual(response.status_code, 200)
        root = ET.fromstring(response.content)  # اگر XML نامعتبر باشد همین‌جا ParseError می‌دهد
        return [el.text for el in root.findall('.//sm:loc', SITEMAP_NS)]

    def test_valid_xml_response(self):
        response = self.client.get('/sitemap.xml')
        self.assertEqual(response.status_code, 200)
        ET.fromstring(response.content)

    def test_contains_static_pages(self):
        locs = ' '.join(self._fetch_locs())
        self.assertIn(reverse('products:home'), locs)
        self.assertIn(reverse('products:product_list'), locs)
        self.assertIn(reverse('blog:list'), locs)

    def test_contains_visible_product_only(self):
        locs = self._fetch_locs()
        product_url = reverse('products:product_detail', args=[self.product.slug])
        self.assertTrue(any(product_url in loc for loc in locs))
        self.assertFalse(any(self.inactive_product.slug in loc for loc in locs))

    def test_contains_top_level_category_only(self):
        locs = self._fetch_locs()
        category_url = reverse('products:category_detail', args=[self.top_category.slug])
        self.assertTrue(any(category_url in loc for loc in locs))
        self.assertFalse(any(self.sub_category.slug in loc for loc in locs))
        self.assertFalse(any(self.inactive_category.slug in loc for loc in locs))

    def test_contains_published_post_only(self):
        locs = self._fetch_locs()
        post_url = reverse('blog:detail', args=[self.published_post.slug])
        self.assertTrue(any(post_url in loc for loc in locs))
        self.assertFalse(any(self.draft_post.slug in loc for loc in locs))


class CanonicalUrlTests(TestCase):
    """SEO Phase D: پوشش تستی منطق canonical کاتالوگ (ProductListView) و صفحات جزئیات/دسته."""

    @classmethod
    def setUpTestData(cls):
        cls.top_category = Category.objects.create(name='دسته کانونیکال', slug='canonical-cat')
        cls.brand = Brand.objects.create(name='برند کانونیکال', slug='canonical-brand')
        cls.other_brand = Brand.objects.create(name='برند دوم کانونیکال', slug='canonical-brand-2')
        cls.product = Product.objects.create(
            name='محصول کانونیکال', slug='canonical-product', erp_code='ERP-CANONICAL-1',
            category=cls.top_category, price=70000, stock=4,
        )
        # برای تست page=2 باید صفحه‌ی دوم واقعاً وجود داشته باشد (PRODUCTS_PER_PAGE=12)، وگرنه
        # Paginator.get_page خودش بی‌صدا به صفحه‌ی ۱ سقوط می‌کند و ادعای تست نادرست می‌شود
        Product.objects.bulk_create([
            Product(
                name=f'محصول کانونیکال پرشمار {i}', slug=f'canonical-bulk-product-{i}',
                erp_code=f'ERP-CANONICAL-BULK-{i}', category=cls.top_category, price=10000, stock=1,
            )
            for i in range(1, 13)
        ])

    def _get(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        canonical_match = CANONICAL_RE.search(content)
        robots_match = ROBOTS_RE.search(content)
        return {
            'canonical': canonical_match.group(1) if canonical_match else None,
            'robots': robots_match.group(1) if robots_match else None,
        }

    def test_bare_shop_page_is_self_canonical_without_querystring(self):
        result = self._get('/shop/')
        self.assertTrue(result['canonical'].endswith('/shop/'))
        self.assertNotIn('?', result['canonical'])

    def test_single_category_kept_in_canonical(self):
        result = self._get(f'/shop/?category={self.top_category.slug}')
        self.assertIn(f'category={self.top_category.slug}', result['canonical'])

    def test_single_brand_kept_in_canonical(self):
        result = self._get(f'/shop/?brand={self.brand.slug}')
        self.assertIn(f'brand={self.brand.slug}', result['canonical'])

    def test_multiple_brands_stripped_from_canonical(self):
        result = self._get(f'/shop/?brand={self.brand.slug}&brand={self.other_brand.slug}')
        self.assertNotIn('brand=', result['canonical'])

    def test_disallowed_params_stripped_from_canonical(self):
        base = f'/shop/?category={self.top_category.slug}'
        extras = ('&sort=price_asc', '&price_min=1000', '&price_max=9000', '&color=red', '&attr_5=value')
        for extra in extras:
            with self.subTest(param=extra):
                canonical = self._get(base + extra)['canonical']
                self.assertIn(f'category={self.top_category.slug}', canonical)
                self.assertNotIn('sort=', canonical)
                self.assertNotIn('price_min=', canonical)
                self.assertNotIn('price_max=', canonical)
                self.assertNotIn('color=', canonical)
                self.assertNotIn('attr_', canonical)

    def test_page_greater_than_one_kept_in_canonical(self):
        result = self._get('/shop/?page=2')
        self.assertIn('page=2', result['canonical'])

    def test_canonical_is_independent_of_parameter_order(self):
        cat = self.top_category.slug
        result_a = self._get(f'/shop/?category={cat}&page=2')
        result_b = self._get(f'/shop/?page=2&category={cat}')
        self.assertEqual(result_a['canonical'], result_b['canonical'])

    def test_search_query_gets_noindex_follow(self):
        result = self._get('/shop/?q=test')
        self.assertEqual(result['robots'], 'noindex, follow')

    def test_non_search_catalog_page_keeps_index_follow(self):
        result = self._get('/shop/')
        self.assertEqual(result['robots'], 'index, follow')

    def test_product_detail_is_self_canonical(self):
        path = reverse('products:product_detail', args=[self.product.slug])
        result = self._get(path)
        self.assertTrue(result['canonical'].endswith(path))

    def test_category_detail_is_self_canonical(self):
        path = reverse('products:category_detail', args=[self.top_category.slug])
        result = self._get(path)
        self.assertTrue(result['canonical'].endswith(path))
