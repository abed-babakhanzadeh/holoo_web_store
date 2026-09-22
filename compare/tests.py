"""تست صفحه‌ی مقایسه — نمایش قیمت مهمان (فاز ۳ سیستم قیمت مهمان)."""

from django.test import TestCase
from django.urls import reverse

from products.models import Category, Product, SiteSettings
from promotions.testing import PromotionTestMixin, make_promotion


class CompareGuestPricingTestBase(PromotionTestMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.category = Category.objects.create(name='تست مقایسه', slug='compare-test-cat')
        cls.product = Product.objects.create(
            name='کالای تست مقایسه', slug='compare-test-product', erp_code='ERP-COMPARE-1',
            category=cls.category, price=120000, stock=5,
        )

    def setUp(self):
        super().setUp()
        SiteSettings.load().save()
        session = self.client.session
        session['compare_ids'] = [self.product.pk]
        session.save()
        self.addCleanup(self.set_guest, mode='price_level', level=1)

    def set_guest(self, *, mode, level=1):
        obj = SiteSettings.load()
        obj.guest_pricing_mode = mode
        obj.guest_price_level = level
        obj.full_clean()
        obj.save()


class CompareListGuestPricingTests(CompareGuestPricingTestBase):
    def test_hidden_mode_shows_login_prompt_with_no_price_digits(self):
        self.set_guest(mode='hide_price')
        response = self.client.get(reverse('compare:list'))
        self.assertContains(response, 'ورود برای مشاهده قیمت')
        self.assertNotContains(response, '120000')

    def test_hidden_mode_with_a_discount_shows_only_the_percent(self):
        self.set_guest(mode='hide_price')
        make_promotion(self.product, percent=25)
        response = self.client.get(reverse('compare:list'))
        self.assertContains(response, 'ورود برای مشاهده قیمت')
        self.assertNotContains(response, '120000')
        self.assertNotContains(response, '90000')          # ۱۲۰۰۰۰ × ۰٫۷۵

    def test_visible_mode_shows_the_real_price_and_no_login_prompt(self):
        self.set_guest(mode='price_level', level=1)
        response = self.client.get(reverse('compare:list'))
        self.assertContains(response, '120000')
        self.assertNotContains(response, 'ورود برای مشاهده قیمت')

    def test_calculated_mode_shows_the_adjusted_price(self):
        obj = SiteSettings.load()
        obj.guest_pricing_mode = 'calculated_price'
        obj.guest_price_level = 1
        obj.guest_adjustment_type = 'percent'
        obj.guest_adjustment_value = 10
        obj.full_clean()
        obj.save()
        response = self.client.get(reverse('compare:list'))
        self.assertContains(response, '132000')            # ۱۲۰۰۰۰ × ۱٫۱


class CompareSearchGuestPricingTests(CompareGuestPricingTestBase):
    def test_hidden_mode_search_result_has_no_price_digits(self):
        self.set_guest(mode='hide_price')
        response = self.client.get(reverse('compare:search'), {'q': 'مقایسه'})
        self.assertContains(response, 'ورود برای مشاهده قیمت')
        self.assertNotContains(response, '120000')

    def test_visible_mode_search_result_shows_the_price(self):
        self.set_guest(mode='price_level', level=1)
        response = self.client.get(reverse('compare:search'), {'q': 'مقایسه'})
        self.assertContains(response, '120000')
