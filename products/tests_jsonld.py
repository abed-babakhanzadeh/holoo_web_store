"""تست‌های داده‌ی ساختاریافته‌ی JSON-LD صفحه‌ی جزئیات محصول (SEO Phase C)."""

import json
import re
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from products.models import Category, Product, SiteSettings

JSONLD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL)


class ProductJsonLdTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.parent_category = Category.objects.create(name='دسته والد جیسون‌ال‌دی', slug='jsonld-parent-cat')
        cls.category = Category.objects.create(
            name='دسته جیسون‌ال‌دی', slug='jsonld-child-cat', parent=cls.parent_category,
        )
        cls.product = Product.objects.create(
            name='محصول جیسون‌ال‌دی', slug='jsonld-product', erp_code='ERP-JSONLD-1',
            product_code='SKU-001', category=cls.category, price=125000, stock=7,
        )
        cls.url = reverse('products:product_detail', args=[cls.product.slug])

    def setUp(self):
        super().setUp()
        self._set_guest_price_visible()
        self.addCleanup(self._set_guest_price_visible)

    @staticmethod
    def _set_guest_price_visible():
        # حالت پیش‌فرضِ نمایش قیمت سطح ۱ بدون تعدیل - قیمت مهمان == product.price خام
        obj = SiteSettings.load()
        obj.guest_pricing_mode = 'price_level'
        obj.guest_price_level = 1
        obj.guest_adjustment_type = 'percent'
        obj.guest_adjustment_value = Decimal('0')
        obj.guest_price_rounding_step = 1
        obj.full_clean()
        obj.save()

    @staticmethod
    def _set_guest_price_hidden():
        obj = SiteSettings.load()
        obj.guest_pricing_mode = 'hide_price'
        obj.full_clean()
        obj.save()

    def _get_jsonld(self, response):
        match = JSONLD_RE.search(response.content.decode())
        self.assertIsNotNone(match, 'تگ <script type="application/ld+json"> در صفحه پیدا نشد')
        return json.loads(match.group(1))


class JsonLdGraphStructureTests(ProductJsonLdTestBase):
    def test_graph_has_context_and_two_nodes(self):
        response = self.client.get(self.url)
        data = self._get_jsonld(response)
        self.assertEqual(data['@context'], 'https://schema.org')
        self.assertIn('@graph', data)
        self.assertEqual(len(data['@graph']), 2)
        types = [node['@type'] for node in data['@graph']]
        self.assertEqual(types, ['Product', 'BreadcrumbList'])

    def test_product_node_has_required_fields(self):
        response = self.client.get(self.url)
        product_node = self._get_jsonld(response)['@graph'][0]
        self.assertEqual(product_node['name'], self.product.name)
        self.assertIn(self.product.slug, product_node['url'])
        self.assertTrue(product_node['image'])
        self.assertTrue(product_node['description'] or product_node['description'] == '')


class JsonLdOffersTests(ProductJsonLdTestBase):
    def test_offers_rendered_with_irr_currency_and_tenfold_price(self):
        # قیمت پایه (سطح ۱، بدون تعدیل/تخفیف) == product.price == 125000 تومان
        response = self.client.get(self.url)
        product_node = self._get_jsonld(response)['@graph'][0]
        self.assertIn('offers', product_node)
        offers = product_node['offers']
        self.assertEqual(offers['@type'], 'Offer')
        self.assertEqual(offers['priceCurrency'], 'IRR')
        self.assertEqual(offers['price'], 1250000)  # 125000 تومان * 10 = ریال

    def test_offers_availability_reflects_stock(self):
        response = self.client.get(self.url)
        offers = self._get_jsonld(response)['@graph'][0]['offers']
        self.assertEqual(offers['availability'], 'https://schema.org/InStock')

        self.product.stock = 0
        self.product.save(update_fields=['stock'])
        response = self.client.get(self.url)
        offers = self._get_jsonld(response)['@graph'][0]['offers']
        self.assertEqual(offers['availability'], 'https://schema.org/OutOfStock')

    def test_offers_completely_absent_when_price_hidden(self):
        self._set_guest_price_hidden()
        response = self.client.get(self.url)
        product_node = self._get_jsonld(response)['@graph'][0]
        self.assertNotIn('offers', product_node)


class JsonLdSkuTests(ProductJsonLdTestBase):
    def test_sku_prefers_product_code(self):
        response = self.client.get(self.url)
        product_node = self._get_jsonld(response)['@graph'][0]
        self.assertEqual(product_node['sku'], 'SKU-001')

    def test_sku_falls_back_to_erp_code_when_product_code_blank(self):
        product = Product.objects.create(
            name='محصول بدون کد کالا', slug='jsonld-no-product-code', erp_code='ERP-JSONLD-2',
            product_code='', category=self.category, price=50000, stock=3,
        )
        response = self.client.get(reverse('products:product_detail', args=[product.slug]))
        product_node = self._get_jsonld(response)['@graph'][0]
        self.assertEqual(product_node['sku'], 'ERP-JSONLD-2')


class JsonLdBrandTests(ProductJsonLdTestBase):
    def test_brand_key_absent_when_product_has_no_brand(self):
        response = self.client.get(self.url)
        product_node = self._get_jsonld(response)['@graph'][0]
        self.assertNotIn('brand', product_node)

    def test_brand_key_present_when_product_has_brand(self):
        from products.models import Brand
        brand = Brand.objects.create(name='برند تست جیسون‌ال‌دی', slug='jsonld-brand')
        self.product.brand = brand
        self.product.save(update_fields=['brand'])
        response = self.client.get(self.url)
        product_node = self._get_jsonld(response)['@graph'][0]
        self.assertEqual(product_node['brand'], {'@type': 'Brand', 'name': 'برند تست جیسون‌ال‌دی'})


class JsonLdBreadcrumbTests(ProductJsonLdTestBase):
    def test_breadcrumb_positions_are_sequential(self):
        response = self.client.get(self.url)
        breadcrumb = self._get_jsonld(response)['@graph'][1]
        positions = [item['position'] for item in breadcrumb['itemListElement']]
        self.assertEqual(positions, list(range(1, len(positions) + 1)))

    def test_breadcrumb_includes_parent_and_category(self):
        response = self.client.get(self.url)
        items = self._get_jsonld(response)['@graph'][1]['itemListElement']
        names = [item['name'] for item in items]
        self.assertIn(self.parent_category.name, names)
        self.assertIn(self.category.name, names)

    def test_breadcrumb_category_links_use_shop_querystring(self):
        response = self.client.get(self.url)
        items = self._get_jsonld(response)['@graph'][1]['itemListElement']
        category_item = next(i for i in items if i['name'] == self.category.name)
        self.assertIn('/shop/?category=', category_item['item'])
        self.assertIn(self.category.slug, category_item['item'])

    def test_last_item_is_product_without_item_field(self):
        response = self.client.get(self.url)
        items = self._get_jsonld(response)['@graph'][1]['itemListElement']
        last = items[-1]
        self.assertEqual(last['name'], self.product.name)
        self.assertNotIn('item', last)


class JsonLdSecurityTests(ProductJsonLdTestBase):
    def test_script_breakout_characters_are_escaped(self):
        malicious_name = 'محصول</script><script>alert(1)</script>'
        self.product.name = malicious_name
        self.product.save(update_fields=['name'])

        response = self.client.get(self.url)
        content = response.content.decode()
        # رشته‌ی خام حمله نباید بدون escape در پاسخ ظاهر شود (وگرنه در مرورگر واقعی از تگ script خارج می‌شد)
        self.assertNotIn('</script><script>alert', content)

        product_node = self._get_jsonld(response)['@graph'][0]
        # با این‌حال بعد از json.loads، مقدار اصلی (بدون هیچ کم‌وکاست) باید دقیقاً برگردد
        self.assertEqual(product_node['name'], malicious_name)

    def test_ampersand_in_name_round_trips_safely(self):
        name_with_amp = 'محصول A & B'
        self.product.name = name_with_amp
        self.product.save(update_fields=['name'])

        response = self.client.get(self.url)
        product_node = self._get_jsonld(response)['@graph'][0]
        self.assertEqual(product_node['name'], name_with_amp)
