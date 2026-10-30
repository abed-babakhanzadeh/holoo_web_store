"""
جستجوی نام محصول: همه‌ی کلمه‌ها در هر جای نام (بدون اهمیتِ ترتیب)، و کادر جستجوی زنده‌ی هدر با لود تنبل ۱۰ تایی.
"""
import re

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from products.models import Category, Product
from products.search import MAX_TERMS, filter_by_name, search_terms

CREAM = 'کرم آبرسانی 100 میلی گرم سانی رنگ قرمز'


class TermsTests(SimpleTestCase):
    def test_terms_are_normalised_deduplicated_and_capped(self):
        self.assertEqual(search_terms('  كرم   سانی  کرم '), ['کرم', 'سانی'])           # ك عربی ← ک، فاصله‌ها، تکرار
        self.assertEqual(search_terms('کرم ۱۰۰'), ['کرم', '100'])
        self.assertEqual(search_terms(''), [])
        self.assertEqual(len(search_terms(' '.join(f'کلمه{i}' for i in range(30)))), MAX_TERMS)


class SearchBase(TestCase):
    def setUp(self):
        self.category = Category.objects.create(name='دسته جستجو', slug='search-cat')
        self.n = 0

    def make(self, name, stock=5, **extra):
        self.n += 1
        return Product.objects.create(name=name, slug=f'search-p-{self.n}', erp_code=f'ERP-SRCH-{self.n}', category=self.category,
                                      price=100000, stock=stock, **extra)

    def names(self, query):
        return sorted(filter_by_name(Product.visible.all(), query).values_list('name', flat=True))


class AllWordsTests(SearchBase):
    def setUp(self):
        super().setUp()
        self.cream = self.make(CREAM)
        self.only_cream = self.make('کرم دست ساده')
        self.only_sani = self.make('شامپو سانی')
        self.other = self.make('مداد رنگی')

    def test_every_word_must_be_in_the_name_not_just_one(self):
        self.assertEqual(self.names('کرم سانی'), [CREAM])

    def test_order_and_position_do_not_matter(self):
        for query in ('سانی کرم', 'قرمز کرم', 'رنگ آبرسانی کرم', 'میلی 100'):
            with self.subTest(query=query):
                self.assertEqual(self.names(query), [CREAM])

    def test_a_single_word_still_works_as_before(self):
        self.assertEqual(self.names('کرم'), sorted([CREAM, 'کرم دست ساده']))

    def test_a_word_that_one_product_lacks_removes_it(self):
        self.assertEqual(self.names('کرم دست قرمز'), [])

    def test_arabic_letters_and_persian_digits_match(self):
        self.assertEqual(self.names('كرم سانی'), [CREAM])
        self.assertEqual(self.names('کرم ۱۰۰'), [CREAM])
        self.assertEqual(self.names('كرم ١٠٠'), [CREAM])

    def test_case_and_extra_spaces_are_ignored(self):
        Product.objects.filter(pk=self.other.pk).update(name='Lipstick Ruby RED', name_normalized='lipstick ruby red')
        self.assertEqual(self.names('  RED   lipstick '), ['Lipstick Ruby RED'])

    def test_empty_or_blank_query_returns_nothing(self):
        self.assertEqual(self.names(''), [])
        self.assertEqual(self.names('   '), [])

    def test_like_wildcards_in_the_query_are_literal(self):
        self.assertEqual(self.names('%'), [])
        self.assertEqual(self.names('کرم _'), [])


class ShopPageTests(SearchBase):
    def test_the_shop_page_uses_the_same_all_words_rule(self):
        cream = self.make(CREAM)
        self.make('کرم دست ساده')
        html = self.client.get(reverse('products:product_list'), {'q': 'سانی کرم'}).content.decode()
        self.assertIn(reverse('products:product_detail', args=[cream.slug]), html)
        self.assertNotIn('search-p-2', html)


def items(html):
    return re.findall(r'href="(/product/[^"]+)"', html)


class LiveSearchTests(SearchBase):
    url = None

    def setUp(self):
        super().setUp()
        self.url = reverse('products:live_search')
        for i in range(25):
            self.make(f'کرم نمونه شماره {i:02d}')
        self.make('شامپو بی‌ربط')

    def get(self, **params):
        return self.client.get(self.url, params)

    def test_first_page_has_ten_rows_the_total_and_a_sentinel_for_the_next_page(self):
        html = self.get(q='کرم').content.decode()
        self.assertEqual(len(items(html)), 10)
        self.assertIn('مشاهده همه 25 نتیجه', html)
        self.assertIn('data-testid="live-search-list"', html)
        self.assertRegex(html, r'hx-get="/search/live/\?q=[^"]+&amp;page=2"')
        self.assertIn('hx-trigger="intersect once"', html)
        self.assertIn('در حال بارگذاری', html)

    def test_following_pages_are_only_rows_without_the_wrapper_and_chain_to_the_end(self):
        second = self.get(q='کرم', page=2).content.decode()
        self.assertEqual(len(items(second)), 10)
        self.assertNotIn('مشاهده همه', second)
        self.assertNotIn('live-search-list', second)
        self.assertIn('page=3', second)
        third = self.get(q='کرم', page=3).content.decode()
        self.assertEqual(len(items(third)), 5)
        self.assertNotIn('live-search-more', third)                     # آخرین صفحه: نگهبان ندارد
        seen = items(self.get(q='کرم').content.decode()) + items(second) + items(third)
        self.assertEqual(len(seen), 25)
        self.assertEqual(len(set(seen)), 25)                            # بدون تکرار بین صفحه‌ها

    def test_a_page_beyond_the_end_returns_nothing_so_the_sentinel_disappears(self):
        response = self.get(q='کرم', page=9)
        self.assertEqual((response.status_code, response.content), (200, b''))

    def test_bad_page_numbers_fall_back_to_the_first_page(self):
        for page in ('abc', '0', '-3', ''):
            with self.subTest(page=page):
                self.assertIn('live-search-list', self.get(q='کرم', page=page).content.decode())

    def test_all_words_rule_applies_and_no_result_message_is_shown(self):
        self.assertEqual(len(items(self.get(q='نمونه 07 کرم').content.decode())), 1)
        self.assertIn('نتیجه‌ای برای «پیکان» یافت نشد', self.get(q='پیکان').content.decode())
        self.assertEqual(self.get(q='').content.decode().strip(), '')

    def test_query_is_url_encoded_inside_the_next_page_link(self):
        html = self.get(q='کرم نمونه').content.decode()
        self.assertIn('q=%DA%A9%D8%B1%D9%85%20%D9%86%D9%85%D9%88%D9%86%D9%87&amp;page=2', html)

    def test_in_stock_products_come_first_then_the_exact_phrase(self):
        Product.objects.all().delete()
        sold_out = self.make('کرم آبرسانی دست', stock=0)             # عبارت پشت‌سرهم ولی ناموجود
        apart = self.make('آبرسانی کرم دست')                          # کلمه‌ها جدا از هم
        exact = self.make('کرم آبرسانی دست و صورت')                   # عبارت پشت‌سرهم
        html = self.get(q='کرم آبرسانی').content.decode()
        slugs = [href.rstrip('/').rsplit('/', 1)[-1] for href in items(html)]
        self.assertEqual(slugs, [exact.slug, apart.slug, sold_out.slug])


class PartialSaveNameTests(SearchBase):
    def test_saving_only_the_name_refreshes_the_search_name_too(self):
        product = self.make('نام قدیمی')
        product.name = 'عطر جدید ۱۲۰'
        product.save(update_fields=['name'])
        self.assertEqual(self.names('عطر'), ['عطر جدید ۱۲۰'])
        self.assertEqual(self.names('قدیمی'), [])
