"""
دکمه‌ی «کپی لینک» مودال اشتراک‌گذاری (static/theme/assets/js/share-link.js): بازخورد دیداری روی دکمه (۲٫۵ ثانیه «کپی شد ✓» و سبز)،
توست، و کپی امن با navigator.clipboard + فال‌بک document.execCommand('copy').
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.test import TestCase
from django.urls import reverse

from returns.tests import ReturnsTestMixin

JS_PATH = Path(settings.BASE_DIR) / 'static/theme/assets/js/share-link.js'

HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync(process.argv[1], 'utf8');

function makeEnv({secure, clipboard, exec}) {
  const timers = [];
  const toasts = [];
  const log = {clipboardCalls: 0, execCalls: 0, cleared: 0};
  class ClassList { constructor() { this.s = new Set(); } toggle(c, on) { on ? this.s.add(c) : this.s.delete(c); } contains(c) { return this.s.has(c); } }
  const input = {value: 'http://localhost:7000/product/test/', focus() { this.focused = true; }, select() { this.selected = true; }};
  const btn = {textContent: 'کپی لینک', classList: new ClassList()};
  const body = {children: [], appendChild(e) { this.children.push(e); }, removeChild(e) { this.children = this.children.filter(x => x !== e); }};
  const document = {
    getElementById: id => ({shareUrlInput: input, shareCopyBtn: btn}[id] || null),
    createElement: () => ({style: {}, setAttribute() {}, select() {}, setSelectionRange() {}}),
    body,
    execCommand: () => { log.execCalls++; if (exec === 'throw') throw new Error('blocked'); return exec; },
  };
  const navigator = clipboard ? {clipboard: {writeText: () => { log.clipboardCalls++; return clipboard === 'reject' ? Promise.reject(new Error('denied')) : Promise.resolve(); }}} : {};
  const window = {isSecureContext: secure, showToast: (m, t) => toasts.push([m, t])};
  const setTimeout = (fn, ms) => { timers.push({fn, ms, live: true}); return timers.length; };
  const clearTimeout = id => { if (timers[id - 1]) { timers[id - 1].live = false; log.cleared++; } };
  vm.runInNewContext(code, {window, document, navigator, setTimeout, clearTimeout});
  return {window, btn, input, toasts, timers, log, body};
}

const tick = () => new Promise(r => setImmediate(r));
const snap = e => ({text: e.btn.textContent, copied: e.btn.classList.contains('is-copied'), failed: e.btn.classList.contains('is-failed'),
  toasts: e.toasts, timers: e.timers.map(t => [t.ms, t.live]), log: e.log, selected: !!e.input.selected, leftovers: e.body.children.length});

(async () => {
  const out = {};
  let e = makeEnv({secure: true, clipboard: 'ok', exec: true});
  e.window.copyShareLink(e.btn); await tick();
  out.success = snap(e);
  e.timers[0].fn();                                   // پایان ۲٫۵ ثانیه
  out.afterReset = snap(e);

  e = makeEnv({secure: true, clipboard: 'reject', exec: true});
  e.window.copyShareLink(e.btn); await tick();
  out.rejectedThenFallback = snap(e);

  e = makeEnv({secure: false, clipboard: 'ok', exec: true});
  e.window.copyShareLink(e.btn); await tick();
  out.insecureContext = snap(e);

  e = makeEnv({secure: false, clipboard: false, exec: false});
  e.window.copyShareLink(e.btn); await tick();
  out.bothFail = snap(e);

  e = makeEnv({secure: false, clipboard: false, exec: 'throw'});
  e.window.copyShareLink(e.btn); await tick();
  out.execThrows = snap(e);

  e = makeEnv({secure: true, clipboard: 'ok', exec: true});
  e.window.copyShareLink(e.btn); await tick();
  e.window.copyShareLink(e.btn); await tick();       // کلیک دوباره‌ی سریع: تایمر قبلی لغو می‌شود
  out.doubleClick = snap(e);

  e = makeEnv({secure: true, clipboard: 'ok', exec: true});
  e.window.copyShareLink(); await tick();             // بدون آرگومان: دکمه با id پیدا می‌شود
  out.noArgument = snap(e);
  console.log(JSON.stringify(out));
})();
"""


class ShareLinkMarkupTests(ReturnsTestMixin, TestCase):
    def setUp(self):
        self.product = self.make_product(self.make_category())

    def page(self):
        response = self.client.get(reverse('products:product_detail', args=[self.product.slug]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_product_page_wires_the_button_to_the_new_script(self):
        html = self.page()
        self.assertRegex(html, r'<button type="button" id="shareCopyBtn" onclick="copyShareLink\(this\)" aria-live="polite" class="share-copy-btn[^"]*"[^>]*>کپی لینک</button>')
        self.assertIn('theme/assets/js/share-link.js', html)
        self.assertLess(html.index('stock-alert.js'), html.index('share-link.js'))            # showToast قبل از آن بارگذاری شود
        self.assertNotIn("btn.textContent = 'کپی شد!'", html)                                 # پیاده‌سازی قدیمی inline برداشته شده
        self.assertNotIn('const btn = event.target', html)

    def test_blog_share_modal_uses_the_same_script(self):
        template = (Path(settings.BASE_DIR) / 'templates/blog/detail.html').read_text(encoding='utf-8')
        self.assertIn('onclick="copyShareLink(this)"', template)
        self.assertIn('share-link.js', template)
        self.assertNotIn("btn.textContent = 'کپی شد!'", template)

    def test_css_has_the_green_success_and_red_failure_states(self):
        css = (Path(settings.BASE_DIR) / 'static/theme/assets/css/app.css').read_text(encoding='utf-8')
        copied = css[css.index('.share-copy-btn.is-copied'):]
        copied = copied[:copied.index('}')]
        self.assertIn('background-image: none', copied)                                      # گرادیان bg-primary-grad را می‌پوشاند
        self.assertIn('#059669', copied)                                                     # emerald-600
        self.assertIn('.share-copy-btn.is-failed', css)

    def test_script_uses_clipboard_api_with_the_legacy_fallback(self):
        js = JS_PATH.read_text(encoding='utf-8')
        for text in ('navigator.clipboard.writeText', 'window.isSecureContext', "document.execCommand('copy')", 'RESET_MS = 2500',
                     "'کپی شد ✓'", 'لینک محصول با موفقیت در کلیپ‌بورد کپی شد'):
            self.assertIn(text, js)


class ShareLinkBehaviourTests(TestCase):
    """ اجرای واقعی share-link.js در node با محیط ساختگی (بدون مرورگر)؛ اگر node نباشد skip می‌شود """
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        node = shutil.which('node')
        cls.results = None
        if node:
            run = subprocess.run([node, '-e', HARNESS, str(JS_PATH)], capture_output=True, text=True, check=True)
            cls.results = json.loads(run.stdout)

    def setUp(self):
        if self.results is None:
            self.skipTest('node در دسترس نیست')

    def test_success_turns_the_button_green_for_2_5_seconds_and_toasts(self):
        r = self.results['success']
        self.assertEqual((r['text'], r['copied'], r['failed']), ('کپی شد ✓', True, False))
        self.assertEqual(r['timers'], [[2500, True]])
        self.assertEqual(r['toasts'], [['لینک محصول با موفقیت در کلیپ‌بورد کپی شد', 'success']])
        self.assertEqual(r['log']['execCalls'], 0)                                           # clipboard API کافی بود

    def test_button_returns_to_its_initial_state_after_the_timeout(self):
        r = self.results['afterReset']
        self.assertEqual((r['text'], r['copied'], r['failed']), ('کپی لینک', False, False))

    def test_rejected_clipboard_api_falls_back_to_execcommand(self):
        r = self.results['rejectedThenFallback']
        self.assertEqual((r['text'], r['copied']), ('کپی شد ✓', True))
        self.assertEqual((r['log']['clipboardCalls'], r['log']['execCalls']), (1, 1))

    def test_insecure_context_skips_the_clipboard_api_and_still_copies(self):
        r = self.results['insecureContext']
        self.assertEqual((r['text'], r['copied']), ('کپی شد ✓', True))
        self.assertEqual((r['log']['clipboardCalls'], r['log']['execCalls']), (0, 1))

    def test_total_failure_shows_an_error_selects_the_link_and_never_claims_success(self):
        for key in ('bothFail', 'execThrows'):
            with self.subTest(case=key):
                r = self.results[key]
                self.assertEqual((r['text'], r['copied'], r['failed']), ('کپی نشد', False, True))
                self.assertTrue(r['selected'])
                self.assertEqual(r['toasts'][0][1], 'error')
                self.assertEqual(r['timers'], [[2500, True]])

    def test_temporary_textarea_is_always_removed(self):
        for key in ('rejectedThenFallback', 'insecureContext', 'bothFail', 'execThrows'):
            with self.subTest(case=key):
                self.assertEqual(self.results[key]['leftovers'], 0)

    def test_double_click_cancels_the_previous_reset_timer(self):
        r = self.results['doubleClick']
        self.assertEqual(r['timers'], [[2500, False], [2500, True]])
        self.assertEqual(r['log']['cleared'], 1)

    def test_works_when_called_without_the_button_argument(self):
        r = self.results['noArgument']
        self.assertEqual((r['text'], r['copied']), ('کپی شد ✓', True))
