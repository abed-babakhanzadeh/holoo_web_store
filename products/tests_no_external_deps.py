"""
صفر وابستگی به سرویس بیرونی در بارگذاری صفحه.

سرورِ کارفرما (ویندوز، ایران) به بعضی دامنه‌های خارجی (unpkg.com، picsum.photos و ...) دسترسی ندارد یا بسیار کند دسترسی دارد.
اسکریپتِ هم‌زمانِ داخل <head> از unpkg لودینگ صفحه را برای همیشه نگه می‌داشت، و `onerror` تصویر استوری که به picsum.photos
می‌رفت (و در نبود دسترسی خودش هم خطا می‌داد) بی‌نهایت دوباره اجرا می‌شد. این تست‌ها جلوی برگشتن هر دو را می‌گیرند:
  - هیچ قالبی اسکریپت/استایل/تصویر/فونت را از دامنه‌ی بیرونی بارگذاری نمی‌کند؛
  - کد اختصاصی JS/CSS هیچ URL بیرونی ندارد (جز namespace استاندارد SVG که درخواست شبکه نیست)؛
  - htmx از فایل محلی می‌آید و onerror استوری فقط یک‌بار اجرا می‌شود.
"""
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

BASE = Path(settings.BASE_DIR)
TEMPLATES = BASE / 'templates'
ASSETS = BASE / 'static' / 'theme' / 'assets'

# تگ‌هایی که منبع می‌کشند و آدرس مطلق (http(s):// یا //) دارند. <iframe> نقشه‌ی صفحه‌ی «تماس با ما» عمداً جداست: آدرسش را مدیر
# در تنظیمات می‌دهد (allowlist دامنه در products/contact.py) و لود صفحه را نگه نمی‌دارد.
EXTERNAL_RESOURCE_TAG = re.compile(
    r'<(script|link|img|source|video|audio)\b[^>]*?\b(?:src|href)\s*=\s*["\'](?:https?:)?//([^"\'/]+)[^"\']*["\'][^>]*>',
    re.I | re.S,
)
NAMESPACE_OK = ('www.w3.org',)


def first_party_assets():
    files = list(ASSETS.glob('js/*.js')) + list((ASSETS / 'js' / 'dependencies').glob('*.js'))
    files += list((ASSETS / 'js' / 'plugin' / 'story-player').glob('*.js')) + list(ASSETS.glob('css/site-logo.css'))
    return files


def strip_comments(text):
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    return re.sub(r'(?m)^\s*//.*$', '', text)


class NoExternalResourceTests(SimpleTestCase):
    def test_no_template_loads_a_script_style_image_or_font_from_another_domain(self):
        offenders = []
        for path in TEMPLATES.rglob('*.html'):
            for match in EXTERNAL_RESOURCE_TAG.finditer(path.read_text(encoding='utf-8', errors='ignore')):
                offenders.append(f'{path.relative_to(BASE)}: <{match.group(1)}> {match.group(2)}')
        self.assertEqual(offenders, [])

    def test_first_party_js_and_css_have_no_external_urls(self):
        offenders = []
        for path in first_party_assets():
            for match in re.finditer(r'https?://([^\s\'"`)>/]+)', strip_comments(path.read_text(encoding='utf-8', errors='ignore'))):
                if match.group(1) not in NAMESPACE_OK:
                    offenders.append(f'{path.relative_to(BASE)}: {match.group(0)}')
        self.assertEqual(offenders, [])

    def test_css_never_imports_or_references_remote_fonts_or_images(self):
        for path in (ASSETS / 'css').glob('*.css'):
            text = strip_comments(path.read_text(encoding='utf-8', errors='ignore'))
            self.assertNotRegex(text, r'@import\s+url\(\s*["\']?https?:', path.name)
            self.assertNotRegex(text, r'url\(\s*["\']?(?:https?:)?//', path.name)


class LocalHtmxTests(SimpleTestCase):
    def test_htmx_is_served_from_the_project_and_is_the_pinned_version(self):
        htmx = ASSETS / 'js' / 'plugin' / 'htmx' / 'htmx.min.js'
        self.assertTrue(htmx.exists())
        text = htmx.read_text(encoding='utf-8')
        self.assertIn('version:"1.9.10"', text)
        self.assertGreater(len(text), 40000)

    def test_both_base_templates_use_the_local_copy(self):
        for name in ('base.html', 'accounts/dashboard_base.html'):
            html = (TEMPLATES / name).read_text(encoding='utf-8')
            self.assertIn("{% static 'theme/assets/js/plugin/htmx/htmx.min.js' %}", html, name)
            self.assertNotIn('unpkg.com', html, name)


class StoryPlayerFallbackTests(SimpleTestCase):
    def setUp(self):
        self.js = (ASSETS / 'js' / 'plugin' / 'story-player' / 'story-player.js').read_text(encoding='utf-8')

    def test_missing_avatar_falls_back_to_an_inline_image_not_a_remote_service(self):
        self.assertNotIn('picsum', strip_comments(self.js))
        self.assertIn("const STORY_AVATAR_FALLBACK = 'data:image/svg+xml,'", self.js)

    def test_the_error_handler_runs_once_so_a_failing_fallback_can_never_loop(self):
        self.assertIn('onerror="this.onerror=null;this.src=this.dataset.fallback"', self.js)
