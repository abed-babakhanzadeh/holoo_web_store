"""
ویرایشگر متن (CKEditor): تنظیمات غنی و فارسی، و ظاهر یکسان متن در ادمین و سایت (static/theme/assets/css/rich-content.css).
"""
from pathlib import Path

from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import CustomUser
from blog.models import BlogCategory, Post
from products.models import Category, Product

CSS = Path(settings.BASE_DIR) / 'static' / 'theme' / 'assets' / 'css' / 'rich-content.css'
BODY = '<h2>عنوان</h2><p><span style="font-size:24px;">متن بزرگ</span></p>'


class EditorConfigTests(TestCase):
    def setUp(self):
        self.config = settings.CKEDITOR_5_CONFIGS['default']
        self.items = self.config['toolbar']['items']

    def test_persian_rtl_interface_and_content(self):
        self.assertEqual(self.config['language'], {'ui': 'fa', 'content': 'fa'})

    def test_toolbar_offers_font_size_family_colors_and_formatting_tools(self):
        for tool in ('fontSize', 'fontFamily', 'fontColor', 'fontBackgroundColor', 'highlight', 'strikethrough', 'removeFormat',
                     'alignment', 'indent', 'outdent', 'todoList', 'insertTable', 'horizontalLine', 'codeBlock', 'sourceEditing',
                     'findAndReplace', 'undo', 'redo'):
            self.assertIn(tool, self.items)

    def test_toolbar_wraps_instead_of_hiding_tools_behind_a_menu(self):
        self.assertTrue(self.config['toolbar']['shouldNotGroupWhenFull'])

    def test_font_size_options_include_larger_sizes_and_keep_the_default(self):
        options = self.config['fontSize']['options']
        self.assertIn('default', options)
        self.assertTrue(any(isinstance(o, int) and o >= 24 for o in options))
        self.assertTrue(any(isinstance(o, int) and o < 16 for o in options))

    def test_line_spacing_styles_are_offered_through_the_style_menu(self):
        self.assertIn('style', self.items)
        classes = {c for d in self.config['style']['definitions'] for c in d['classes']}
        self.assertEqual(classes, {'lh-tight', 'lh-medium', 'lh-loose', 'lh-xloose'})

    def test_existing_image_alignment_styles_are_kept_for_old_content(self):
        self.assertEqual(self.config['image']['styles'], ['full', 'alignLeft', 'alignCenter', 'alignRight'])


class SharedLookTests(TestCase):
    def test_site_and_admin_editor_share_the_same_rules(self):
        css = CSS.read_text(encoding='utf-8')
        for selector in ('.rich-content h2', '.ck-content h2', '.rich-content blockquote', '.ck-content blockquote',
                         '.rich-content table td', '.ck-content table td', '.rich-content figure.image', '.ck-content figure.image'):
            self.assertIn(selector, css)
        self.assertIn('direction: rtl', css)
        self.assertIn('font-size: 16px', css)

    def test_line_spacing_classes_exist_for_site_and_admin(self):
        css = CSS.read_text(encoding='utf-8')
        for name in ('lh-tight', 'lh-medium', 'lh-loose', 'lh-xloose'):
            self.assertIn(f'.rich-content .{name}', css)
            self.assertIn(f'.ck-content .{name}', css)

    def test_storefront_and_admin_load_the_stylesheet(self):
        self.assertContains(self.client.get(reverse('products:home')), 'theme/assets/css/rich-content.css')
        admin = CustomUser.objects.create_superuser(phone_number='09125557001')
        self.client.force_login(admin)
        self.assertContains(self.client.get(reverse('admin:index')), 'theme/assets/css/rich-content.css')


class RenderingTests(TestCase):
    def setUp(self):
        category = Category.objects.create(name='دسته ویرایشگر', slug='editor-cat', short_description=BODY)
        self.product = Product.objects.create(name='کالای ویرایشگر', slug='editor-product', erp_code='ERP-ED-1', category=category,
                                              price=100000, stock=5, additional_description=BODY)
        self.category = category

    def test_product_additional_description_uses_the_shared_class_and_keeps_font_size(self):
        html = self.client.get(reverse('products:product_detail', args=[self.product.slug])).content.decode()
        self.assertRegex(html, r'class="[^"]*rich-content[^"]*"[^>]*>\s*<h2>عنوان</h2>')
        self.assertIn('font-size:24px', html)
        self.assertNotIn('prose dark:prose-invert max-w-none text-sm', html)

    def test_intro_tab_uses_the_same_typography_and_keeps_paragraphs(self):
        Product.objects.filter(pk=self.product.pk).update(description='خط اول\n\nخط دوم')
        html = self.client.get(reverse('products:product_detail', args=[self.product.slug])).content.decode()
        self.assertRegex(html, r'rich-content text-justify"[^>]*>\s*<p>خط اول</p>\s*<p>خط دوم</p>')

    def test_category_description_uses_the_shared_class(self):
        html = self.client.get(reverse('products:category_detail', args=[self.category.slug])).content.decode()
        self.assertIn('<div class="rich-content"><h2>عنوان</h2>', html)

    def test_blog_post_body_uses_the_shared_class(self):
        blog_category = BlogCategory.objects.create(name='دسته وبلاگ', slug='editor-blog-cat')
        post = Post.objects.create(title='مقاله', slug='editor-post', body=BODY, category=blog_category, status='published',
                                   published_at=timezone.now())
        html = self.client.get(reverse('blog:detail', args=[post.slug])).content.decode()
        self.assertIn('<div class="rich-content"><h2>عنوان</h2>', html)
