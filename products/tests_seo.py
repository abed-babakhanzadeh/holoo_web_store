"""تست‌های SEO Phase B2: robots.txt و sitemap.xml."""

import xml.etree.ElementTree as ET

from django.test import TestCase
from django.urls import reverse

from blog.models import Post
from products.models import Category, Product

SITEMAP_NS = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}


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
