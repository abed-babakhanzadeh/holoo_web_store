"""
رفع «صفحه در صفحه» و پرش به بالا هنگام «افزودن به سبد» / قلب (static/theme/assets/js/htmx-guard.js، cart/views.py،
templates/products/product_list.html).

ریشه‌ها:
  - کاربرِ واردشده‌ی تأییدنشده دکمه‌ی سبد را می‌بیند (قیمت برایش نمایش داده می‌شود)؛ AddToCartView او را به login?next=<کالا> می‌فرستاد،
    login کاربر واردشده را به صفحه‌ی کالا برمی‌گرداند و htmx کل صفحه‌ی کالا را داخل دکمه swap می‌کرد (+ اجرای دوباره‌ی اسکریپت‌های
    سراسری و خطای «Identifier 'swiper' has already been declared»).
  - hx-on::after-settle روی گرید محصولات رویدادهای حبابیِ هر swap داخل گرید (سبد، قلب، مقایسه) را هم می‌گرفت و صفحه را به بالا می‌برد.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.test import TestCase
from django.urls import reverse

from accounts.models import CustomUser
from accounts.testing import make_approved_user
from returns.tests import ReturnsTestMixin

BASE = Path(settings.BASE_DIR)
GUARD_JS = BASE / 'static/theme/assets/js/htmx-guard.js'

HARNESS = r"""
const fs = require('fs'); const vm = require('vm');
const code = fs.readFileSync(process.argv[1], 'utf8');
const listeners = {}; const appended = []; const navigations = [];
const document = {
  body: {appendChild: e => appended.push(e)},
  addEventListener: (name, fn) => { (listeners[name] = listeners[name] || []).push(fn); },
  createElement: () => ({style: {}, setAttribute() {}, parentNode: null}),
};
const window = {location: {href: 'https://shop.test/shop/', assign: u => navigations.push(u)}};
vm.runInNewContext(code, {document, window, setTimeout: () => 0});
const fire = (name, detail) => { const evt = {detail}; (listeners[name] || []).forEach(fn => fn(evt)); return evt; };
const swap = (text, url, elt) => fire('htmx:beforeSwap', {xhr: {responseText: text, responseURL: url}, elt, shouldSwap: true}).detail;
const closest = hit => ({closest: sel => (hit && sel === '[hx-select]') ? {} : null});
const out = {};
out.fragment = swap('<div id="cart-btn-1">x</div>', 'https://shop.test/cart/add/1/', closest(false)).shouldSwap;
out.fullDocRedirected = (() => { const d = swap('<!DOCTYPE html><html>...', 'https://shop.test/product/x/', closest(false)); return [d.shouldSwap, navigations.slice()]; })();
navigations.length = 0;
out.fullDocSameUrl = (() => { const d = swap('  <html lang="fa">', 'https://shop.test/shop/', closest(false)); return [d.shouldSwap, navigations.slice()]; })();
out.fullDocWithHxSelect = swap('<!doctype html><html>', 'https://shop.test/p/?sort=1', closest(true)).shouldSwap;
out.noXhr = (() => { const d = fire('htmx:beforeSwap', {shouldSwap: true}).detail; return d.shouldSwap; })();
out.emptyBody = swap('', 'https://shop.test/x/', closest(false)).shouldSwap;
fire('holooToast', {message: 'حساب شما تأیید نشده', type: 'error'});
fire('holooToast', {});
out.toasts = appended.map(e => [e.textContent, /b91c1c/.test(e.style.cssText)]);
console.log(JSON.stringify(out));
"""


class GuardBehaviourTests(TestCase):
    """ اجرای واقعی htmx-guard.js در node با محیط ساختگی؛ اگر node نباشد skip می‌شود """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        node = shutil.which('node')
        cls.result = None
        if node:
            run = subprocess.run([node, '-e', HARNESS, str(GUARD_JS)], capture_output=True, text=True, check=True, encoding='utf-8')
            cls.result = json.loads(run.stdout)

    def setUp(self):
        if self.result is None:
            self.skipTest('node در دسترس نیست')

    def test_a_partial_response_is_swapped_normally(self):
        self.assertTrue(self.result['fragment'])
        self.assertTrue(self.result['emptyBody'])
        self.assertTrue(self.result['noXhr'])

    def test_a_redirected_full_page_is_never_swapped_into_an_element_and_we_navigate_there(self):
        self.assertEqual(self.result['fullDocRedirected'], [False, ['https://shop.test/product/x/']])

    def test_a_full_page_for_the_same_url_is_blocked_without_a_reload_loop(self):
        self.assertEqual(self.result['fullDocSameUrl'], [False, []])

    def test_elements_with_hx_select_keep_receiving_full_documents(self):
        self.assertTrue(self.result['fullDocWithHxSelect'])

    def test_server_toasts_are_shown_and_empty_ones_ignored(self):
        self.assertEqual(self.result['toasts'], [['حساب شما تأیید نشده', True]])


class CartHtmxResponsesTests(ReturnsTestMixin, TestCase):
    def setUp(self):
        self.product = self.make_product(self.make_category())
        self.url = reverse('cart:add_to_cart', args=[self.product.pk])

    def post(self, **headers):
        return self.client.post(self.url, {}, **headers)

    def test_unapproved_signed_in_user_gets_a_toast_and_no_redirect(self):
        user = CustomUser.objects.create_user(phone_number='09120000601', first_name='ا', last_name='ب', national_code='0012345678')
        self.client.force_login(user)
        response = self.post(HTTP_HX_REQUEST='true')
        self.assertEqual(response.status_code, 204)
        self.assertFalse(response.has_header('Location'))
        toast = json.loads(response['HX-Trigger'])['holooToast']
        self.assertEqual(toast['type'], 'error')
        self.assertIn('تأیید نشده', toast['message'])
        self.assertFalse(response.content)

    def test_rejected_user_gets_the_rejection_wording(self):
        user = CustomUser.objects.create_user(phone_number='09120000602', first_name='ا', last_name='ب', national_code='0012345679')
        CustomUser.objects.filter(pk=user.pk).update(approval_status='REJECTED')
        self.client.force_login(CustomUser.objects.get(pk=user.pk))
        toast = json.loads(self.post(HTTP_HX_REQUEST='true')['HX-Trigger'])['holooToast']
        self.assertIn('تأیید نشد', toast['message'])

    def test_guest_htmx_request_is_sent_to_login_with_a_full_navigation(self):
        response = self.post(HTTP_HX_REQUEST='true')
        self.assertEqual(response.status_code, 204)
        self.assertFalse(response.has_header('Location'))
        target = response['HX-Redirect']
        self.assertTrue(target.startswith(reverse('accounts:login_view')))
        self.assertIn(self.product.slug, target)

    def test_plain_non_htmx_requests_still_redirect_like_before(self):
        response = self.post()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].startswith(reverse('accounts:login_view')))

    def test_every_cart_action_shares_the_behaviour(self):
        for name in ('decrease_cart', 'remove_from_cart'):
            with self.subTest(name=name):
                try:
                    url = reverse(f'cart:{name}', args=[self.product.pk])
                except Exception:
                    self.skipTest(f'{name} نام دیگری دارد')
                response = self.client.post(url, {}, HTTP_HX_REQUEST='true')
                self.assertEqual((response.status_code, response.has_header('Location')), (204, False))
                self.assertIn('HX-Redirect', response)

    def test_an_approved_user_still_adds_normally(self):
        self.client.force_login(make_approved_user('09120000603', price_level=1))
        response = self.post(HTTP_HX_REQUEST='true')
        self.assertEqual(response.status_code, 200)
        self.assertIn('cartUpdated', response['HX-Trigger'])
        self.assertContains(response, f'cart-btn-{self.product.pk}')


class TemplateWiringTests(TestCase):
    def read(self, relative):
        return (BASE / relative).read_text(encoding='utf-8')

    def test_both_base_templates_load_the_guard_right_after_htmx(self):
        for name in ('templates/base.html', 'templates/accounts/dashboard_base.html'):
            html = self.read(name)
            self.assertRegex(html, r"htmx\.min\.js' %\}\"></script>\s*<script src=\"\{% static 'theme/assets/js/htmx-guard\.js' %\}", name)

    def test_grid_scroll_to_top_only_fires_for_the_grid_itself(self):
        html = self.read('templates/products/product_list.html')
        self.assertIn('hx-on::after-settle="if (event.target === this) window.scrollTo', html)
        self.assertNotIn('hx-on::after-settle="window.scrollTo', html)

    def test_missing_brand_logos_are_hidden_once_not_shown_broken(self):
        for name in ('templates/products/partials/brand_card.html', 'templates/products/product_detail.html'):
            html = self.read(name)
            self.assertRegex(html, r'brand\.logo\.url \}\}"[^>]*onerror="this\.onerror=null;this\.style\.visibility=\'hidden\'"', name)
