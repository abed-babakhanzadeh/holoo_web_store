"""
اصلاحات ظاهری ۱۷ اکتبر ۲۰۲۶: جداکننده‌ی سه‌رقمی روی قیمت‌ها، تعویض خودکار اسلایدر قابل‌تنظیم، ظاهر استوری دیده‌شده،
فیلترهای فعال فروشگاه، و پنهان‌بودن شناسه‌ی بلند کالا. (فایل‌های JS/CSS با خواندن متن بررسی می‌شوند؛ رفتار واقعی در مرورگر دیده شد.)
"""
import re
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from accounts.testing import make_approved_user
from products.models import Category, HeroSlide, Product, SiteSettings

BASE = Path(settings.BASE_DIR)


def read(relative):
    return (BASE / relative).read_text(encoding='utf-8')


def make_product(**extra):
    category = Category.objects.get_or_create(slug='ui-polish-cat', defaults={'name': 'دسته'})[0]
    data = dict(name='کرم تست قیمت', slug='ui-polish-p', erp_code='ERP-UI-1', category=category, price=1234567, price2=1234567,
                price3=1234567, stock=9, product_code='00123')
    data.update(extra)
    return Product.objects.create(**data)


class ThousandSeparatorTests(TestCase):
    def setUp(self):
        self.product = make_product()
        self.user = make_approved_user('09125550201', price_level=2)

    def test_shop_card_and_product_page_show_grouped_prices(self):
        self.client.force_login(self.user)
        for url in (reverse('products:product_list'), reverse('products:product_detail', args=[self.product.slug])):
            html = self.client.get(url).content.decode()
            self.assertIn('1,234,567', html, url)
            self.assertNotRegex(html, r'(?<![\d,])1234567(?![\d,])(?=\s*(<span|تومان))', url)

    def test_no_price_template_still_uses_plain_floatformat_for_display(self):
        """ floatformat:0 فقط برای مقدار ورودی/ماشینی (value/data-*) مجاز است؛ هر نمایش «… تومان» باید money باشد """
        offenders = []
        for path in (BASE / 'templates').rglob('*.html'):
            for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), start=1):
                if 'floatformat:0' in line and ('تومان' in line or '<span' in line) and 'value=' not in line and 'data-' not in line:
                    offenders.append(f'{path.relative_to(BASE)}:{number}')
        self.assertEqual(offenders, [])

    def test_wallet_and_cart_templates_load_the_money_filter(self):
        for relative in ('templates/cart/partials/nav_cart_body.html', 'templates/wallet/wallet_dashboard.html',
                         'templates/orders/partials/invoice.html', 'templates/products/partials/product_card.html'):
            self.assertRegex(read(relative), r'\{%\s*load[^%]*\bmoney\b', relative)

    def test_money_filter_groups_and_rounds(self):
        from orders.templatetags.money import money
        self.assertEqual(money(1234567), '1,234,567')
        self.assertEqual(money('1999999.6'), '2,000,000')
        self.assertEqual(money(0), '0')
        self.assertEqual(money(None), '')


class HeroAutoplayTests(TestCase):
    def setUp(self):
        self.addCleanup(cache.delete, SiteSettings.CACHE_KEY)
        HeroSlide.objects.create(image='home/slider/a.jpg', alt_text='الف', order=1, is_active=True)
        HeroSlide.objects.create(image='home/slider/b.jpg', alt_text='ب', order=2, is_active=True)

    def set_seconds(self, value):
        SiteSettings.objects.update_or_create(pk=1, defaults={'hero_autoplay_seconds': value})
        cache.delete(SiteSettings.CACHE_KEY)

    def test_default_is_five_seconds_and_is_rendered_on_the_slider(self):
        self.assertEqual(SiteSettings.load().hero_autoplay_seconds, 5)
        html = self.client.get(reverse('products:home')).content.decode()
        self.assertIn('data-autoplay="5"', html)

    def test_admin_value_is_used_and_zero_turns_it_off(self):
        self.set_seconds(8)
        self.assertIn('data-autoplay="8"', self.client.get(reverse('products:home')).content.decode())
        self.set_seconds(0)
        self.assertIn('data-autoplay="0"', self.client.get(reverse('products:home')).content.decode())

    def test_value_is_validated_and_exposed_in_the_admin(self):
        settings_obj = SiteSettings.load()
        settings_obj.hero_autoplay_seconds = 61
        with self.assertRaises(ValidationError) as ctx:
            settings_obj.full_clean()
        self.assertIn('hero_autoplay_seconds', ctx.exception.message_dict)
        from products.admin import SiteSettingsAdmin
        flat = [field for _title, options in SiteSettingsAdmin.fieldsets for field in options['fields']]
        self.assertIn('hero_autoplay_seconds', flat)

    def test_script_enables_autoplay_only_when_seconds_are_positive(self):
        js = read('static/theme/assets/js/dependencies/swiper-script.js')
        self.assertIn('heroCarouselEl.dataset.autoplay', js)
        self.assertIn('autoplay: heroAutoplaySeconds > 0', js)
        self.assertIn('disableOnInteraction: false', js)


class StoryAndFilterStyleTests(TestCase):
    def test_seen_story_keeps_a_vivid_avatar_with_only_a_calm_ring(self):
        css = read('static/theme/assets/js/plugin/story-player/styles.css')
        self.assertNotIn('opacity: .55', css)
        self.assertNotIn('grayscale', css)
        self.assertNotRegex(css, r'\.story-avatar\.viewed img\s*\{')              # عکس دیده‌شده دست‌نخورده می‌ماند
        self.assertNotIn('story-username {\n    opacity', css)
        self.assertIn('.story-avatar.viewed {', css)

    def test_active_filter_boxes_are_filled_with_a_checkmark(self):
        js = read('static/theme/assets/js/dependencies/app.js')
        self.assertEqual(len(re.findall(r'setFilterBoxState\(box, \w+\);', js)), 5)          # برند، ارسال رایگان، تخفیف، موجود، مشخصات
        self.assertIn("box.classList.toggle('is-checked', active);", js)
        self.assertIn("box.classList.toggle('bg-white', !active);", js)
        self.assertIn("el.classList.toggle('is-active', active);", js)
        css = read('static/theme/assets/css/shop-filters.css')
        self.assertIn('.brand-checkbox-box.is-checked', css)
        self.assertIn('background-color: var(--color-primary', css)
        self.assertIn('[data-filter-color].is-active', css)

    def test_shop_page_links_the_filter_stylesheet_and_new_script_version(self):
        html = self.client.get(reverse('products:product_list')).content.decode()
        self.assertIn('theme/assets/css/shop-filters.css', html)
        self.assertIn('dependencies/app.js?v=6', html)


def visible_html(response):
    """ HTML بدون اسکریپت‌ها (JSON-LD سئو «sku» دارد و دیده نمی‌شود) """
    return re.sub(r'<script.*?</script>', '', response.content.decode(), flags=re.S)


class ProductCodeVisibilityTests(TestCase):
    def test_product_page_shows_the_short_code_and_never_the_long_erp_id(self):
        product = make_product(erp_code='bBAHNA1mckd4dh4O-LONG', product_code='00123')
        html = visible_html(self.client.get(reverse('products:product_detail', args=[product.slug])))
        self.assertIn('کد محصول: 00123', html)
        self.assertNotIn('bBAHNA1mckd4dh4O-LONG', html)

    def test_product_without_a_short_code_shows_no_code_line(self):
        product = make_product(erp_code='bBAHNA1mckd4dh4O-LONG', product_code=None)
        html = visible_html(self.client.get(reverse('products:product_detail', args=[product.slug])))
        self.assertNotIn('کد محصول', html)
        self.assertNotIn('bBAHNA1mckd4dh4O-LONG', html)
