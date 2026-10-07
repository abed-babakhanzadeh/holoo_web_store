"""
افکت پرواز محصول به سبد: تنظیم سایت، دکمه‌های علامت‌خورده، و داده‌ی پنجره‌ی کوچک سبد (تعداد روی عکس، تازه‌شدن با nav_cart).
خودِ انیمیشن در مرورگر بررسی می‌شود؛ اینجا فقط قراردادهای سمت سرور که JS روی آن‌ها تکیه دارد.
"""
import re

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from accounts.testing import make_approved_user
from accounts.models import CustomUser
from products.models import Category, Product, SiteSettings


class CartFlyTests(TestCase):
    def setUp(self):
        self.addCleanup(cache.delete, SiteSettings.CACHE_KEY)
        self.user = make_approved_user('09125550001')
        category = Category.objects.create(name='افکت', slug='fly-cat')
        self.p1 = Product.objects.create(name='کالای یک', slug='fly-p1', erp_code='FLY-1', category=category, price=100000, stock=50)
        self.p2 = Product.objects.create(name='کالای دو', slug='fly-p2', erp_code='FLY-2', category=category, price=200000, stock=50)
        self.client.force_login(self.user)

    def add(self, product, **extra):
        return self.client.post(reverse('cart:add_to_cart', args=[product.id]), extra, HTTP_HX_REQUEST='true')

    def set_setting(self, **values):
        SiteSettings.objects.update_or_create(pk=1, defaults=values)
        cache.delete(SiteSettings.CACHE_KEY)

    def test_defaults_are_on_and_the_effect_ignores_the_os_reduced_motion_flag(self):
        settings_row = SiteSettings.load()
        self.assertTrue(settings_row.cart_fly_animation_enabled)
        self.assertFalse(settings_row.cart_fly_respect_reduced_motion)

    def test_the_body_carries_the_switches_the_script_reads(self):
        html = self.client.get(reverse('products:product_list')).content.decode()
        self.assertIn('data-cart-fly="1"', html)
        self.assertIn('data-cart-fly-respect-motion="0"', html)
        self.set_setting(cart_fly_animation_enabled=False, cart_fly_respect_reduced_motion=True)
        html = self.client.get(reverse('products:product_list')).content.decode()
        self.assertIn('data-cart-fly="0"', html)
        self.assertIn('data-cart-fly-respect-motion="1"', html)

    def test_hover_popup_is_on_by_default_for_logged_in_users_only(self):
        self.assertTrue(SiteSettings.load().cart_hover_popup_enabled)
        self.assertIn('data-cart-hover="1"', self.client.get(reverse('products:product_list')).content.decode())
        self.client.logout()
        self.assertIn('data-cart-hover="0"', self.client.get(reverse('products:product_list')).content.decode())

    def test_hover_popup_can_be_switched_off_independently_of_the_fly_effect(self):
        self.set_setting(cart_hover_popup_enabled=False)
        html = self.client.get(reverse('products:product_list')).content.decode()
        self.assertIn('data-cart-hover="0"', html)
        self.assertIn('data-cart-fly="1"', html)

    def test_assets_are_loaded_once(self):
        html = self.client.get(reverse('products:product_list')).content.decode()
        self.assertEqual(html.count('theme/assets/js/cart-fly.js'), 1)
        self.assertEqual(html.count('theme/assets/css/cart-fly.css'), 1)

    def test_add_buttons_are_marked_with_product_id_and_image(self):
        html = self.add(self.p1, compact='true').content.decode()
        self.assertEqual(html.count('data-cart-fly-add'), 1)      # فقط «+» (استپر) چون محصول همین الان اضافه شده
        self.assertIn(f'data-fly-product="{self.p1.id}"', html)
        self.assertIn('data-fly-image="', html)

    def test_the_first_add_button_is_marked_too(self):
        html = self.client.get(reverse('cart:cart_button_status', args=[self.p1.id]), {'compact': 'true'}).content.decode()
        self.assertIn('data-cart-fly-add', html)

    def test_drawer_buttons_are_not_marked_so_they_never_animate(self):
        self.add(self.p1)
        html = self.client.get(reverse('cart:nav_cart'), HTTP_HX_REQUEST='true').content.decode()
        drawer = html.split('id="offcanvas-cart-body"', 1)[1].split('id="cart-fly-data"', 1)[0]
        self.assertNotIn('data-cart-fly-add', drawer)

    def test_popup_data_lists_items_with_quantity_on_repeated_adds(self):
        for _ in range(3):
            self.add(self.p1)
        self.add(self.p2)
        html = self.client.get(reverse('cart:nav_cart'), HTTP_HX_REQUEST='true').content.decode()
        self.assertIn('id="cart-fly-data"', html)
        self.assertIn('hx-swap-oob="true"', html.split('id="cart-fly-data"', 1)[1][:80])
        self.assertIn('data-qty="4"', html)
        self.assertIn(f'data-product-id="{self.p1.id}"', html)
        self.assertEqual(re.findall(r'<span class="cfp-qty">(\d+)</span>', html), ['3'])      # فقط ردیفِ چندتایی شمارنده دارد
        self.assertIn(reverse('orders:checkout'), html)

    def test_the_popup_data_is_empty_for_an_empty_cart(self):
        html = self.client.get(reverse('cart:nav_cart'), HTTP_HX_REQUEST='true').content.decode()
        self.assertIn('data-qty="0"', html)
        self.assertNotIn('cfp-items', html)

    def test_more_than_eleven_lines_show_a_plus_n_tile(self):
        category = Category.objects.create(name='بیشتر', slug='fly-more')
        for i in range(13):
            product = Product.objects.create(name=f'کالا {i}', slug=f'fly-more-{i}', erp_code=f'FLY-M{i}', category=category,
                                             price=1000, stock=5)
            self.add(product)
        html = self.client.get(reverse('cart:nav_cart'), HTTP_HX_REQUEST='true').content.decode()
        self.assertEqual(html.count('class="cfp-item"'), 11)
        self.assertIn('+2', html)

    def test_an_unapproved_customer_gets_no_success_response_the_script_could_animate(self):
        unapproved = CustomUser.objects.create_user('09125550002', first_name='الف', last_name='ب', national_code='0012345678')
        self.client.force_login(unapproved)
        response = self.add(self.p1)
        self.assertEqual(response.status_code, 204)      # JS فقط روی ۲۰۰ انیمیشن می‌گیرد

    def test_javascript_parses(self):
        import shutil
        import subprocess
        from pathlib import Path
        from django.conf import settings
        node = shutil.which('node')
        if not node:
            self.skipTest('node نصب نیست')
        path = Path(settings.BASE_DIR) / 'static/theme/assets/js/cart-fly.js'
        result = subprocess.run([node, '--check', str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
