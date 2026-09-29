"""
تست‌های اسکنر تصاویر محصولات (products/services.py::sync_product_images) و تصویر پیش‌فرض
(Product.main_image_url) - فاز «Product Image Scanner Refactor & Default Placeholder».

عمداً هیچ تستی به main_image واقعی (فایل تصویر معتبر برای Pillow) وابسته نیست جز تست‌های
main_image_url؛ چون sync_product_images فقط نام فایل روی دیسک را می‌خواند و مسیر رشته‌ای را
مستقیماً روی FieldFile می‌نشاند (بدون اعتبارسنجی محتوای فایل)، دقیقاً هم‌رفتار با پیاده‌سازی قبلی.
"""
import itertools
import shutil
import tempfile
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.templatetags.static import static
from django.test import TestCase, override_settings

from products.models import Product, ProductImage
from products.services import sync_product_images

_seq = itertools.count(1)

TINY_GIF = b'GIF87a\x01\x00\x01\x00\x80\x01\x00\x00\x00\x00ccc\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;'


def _make_product(erp_code=None, product_code=None):
    n = next(_seq)
    return Product.objects.create(
        name=f'کالای تست اسکنر {n}',
        slug=f'image-sync-test-{n}',
        erp_code=erp_code or f'ERP-IMGSYNC-{n}',
        product_code=product_code,
    )


class ImageSyncTestBase(TestCase):
    def setUp(self):
        super().setUp()
        self.media_root = tempfile.mkdtemp(prefix='products-catalog-test-')
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=self.media_root)
        override.enable()
        self.addCleanup(override.disable)

    def _catalog_dir(self):
        path = Path(self.media_root) / 'products' / 'catalog'
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _drop_file(self, filename, content=b'fake-image-bytes'):
        (self._catalog_dir() / filename).write_bytes(content)


class ProductCodeMatchingTests(ImageSyncTestBase):
    """ محور اصلی این فاز: تطبیق بر مبنای product_code («کد کالا»)، با Fallback به erp_code. """

    def test_matches_main_image_by_product_code(self):
        product = _make_product(product_code='00215002')
        self._drop_file('00215002-1.jpg')

        result = sync_product_images()

        product.refresh_from_db()
        self.assertEqual(product.main_image.name, 'products/catalog/00215002-1.jpg')
        self.assertEqual(result['matched'], 1)
        self.assertEqual(result['unmatched'], [])
        self.assertEqual(result['ambiguous'], [])

    def test_matches_gallery_image_by_product_code(self):
        product = _make_product(product_code='125')
        self._drop_file('125-1.jpg')
        self._drop_file('125-2.png')

        sync_product_images()

        product.refresh_from_db()
        self.assertEqual(product.main_image.name, 'products/catalog/125-1.jpg')
        gallery = ProductImage.objects.get(product=product, order=2)
        self.assertEqual(gallery.image.name, 'products/catalog/125-2.png')

    def test_falls_back_to_erp_code_for_legacy_filenames(self):
        """ فایل‌های قدیمی که هنوز با نام قدیمی (ErpCode) در پوشه مانده‌اند باید همچنان کار کنند. """
        product = _make_product(erp_code='bBAHNA1mckd4dh4O', product_code='00215002')
        self._drop_file('bBAHNA1mckd4dh4O-1.jpg')

        result = sync_product_images()

        product.refresh_from_db()
        self.assertEqual(product.main_image.name, 'products/catalog/bBAHNA1mckd4dh4O-1.jpg')
        self.assertEqual(result['matched'], 1)
        self.assertEqual(result['unmatched'], [])

    def test_product_code_takes_precedence_over_erp_code(self):
        """ اگر یک کد هم با product_code یک محصول و هم با erp_code محصول دیگری تطبیق داشت،
        محصولِ صاحبِ product_code باید برنده شود - نه Fallback. """
        winner = _make_product(product_code='SHARED-CODE')
        loser = _make_product(erp_code='SHARED-CODE')
        self._drop_file('SHARED-CODE-1.jpg')

        sync_product_images()

        winner.refresh_from_db()
        loser.refresh_from_db()
        self.assertEqual(winner.main_image.name, 'products/catalog/SHARED-CODE-1.jpg')
        self.assertFalse(loser.main_image)

    def test_unmatched_when_code_not_found_by_either_field(self):
        self._drop_file('NO-SUCH-CODE-1.jpg')

        result = sync_product_images()

        self.assertEqual(result['unmatched'], ['NO-SUCH-CODE-1.jpg'])
        self.assertEqual(result['ambiguous'], [])
        self.assertEqual(result['matched'], 0)

    def test_ambiguous_duplicate_product_code_is_skipped_and_reported(self):
        """ چون product_code یکتا نیست، دو محصول با یک product_code یکسان نباید هیچ‌کدام
        عکس بگیرند - فایل باید در دسته‌ی جداگانه‌ی ambiguous گزارش شود، نه unmatched و نه
        این‌که به‌صورت غیرقطعی به یکی از آن دو وصل شود. """
        p1 = _make_product(product_code='DUPLICATE-CODE')
        p2 = _make_product(product_code='DUPLICATE-CODE')
        self._drop_file('DUPLICATE-CODE-1.jpg')

        result = sync_product_images()

        p1.refresh_from_db()
        p2.refresh_from_db()
        self.assertFalse(p1.main_image)
        self.assertFalse(p2.main_image)
        self.assertEqual(result['ambiguous'], ['DUPLICATE-CODE-1.jpg'])
        self.assertEqual(result['unmatched'], [])
        self.assertEqual(result['matched'], 0)

    def test_blank_product_code_does_not_interfere_with_erp_code_fallback(self):
        """ محصولی با product_code خالی/تهی نباید کوئری product_code را به اشتباه تطبیق دهد؛
        فایل باید همچنان با erp_code آن محصول (یا محصول دیگر) تطبیق پیدا کند. """
        product = _make_product(erp_code='ERP-BLANK-CODE', product_code='')
        self._drop_file('ERP-BLANK-CODE-1.jpg')

        sync_product_images()

        product.refresh_from_db()
        self.assertEqual(product.main_image.name, 'products/catalog/ERP-BLANK-CODE-1.jpg')


class FileExtensionSupportTests(ImageSyncTestBase):
    """ FILENAME_RE باید jpg/jpeg/png و webp (فرمت مدرن وب) را با حروف بزرگ/کوچک بشناسد. """

    def test_matches_main_image_with_webp_extension(self):
        product = _make_product(product_code='00215002')
        self._drop_file('00215002-1.webp')

        result = sync_product_images()

        product.refresh_from_db()
        self.assertEqual(product.main_image.name, 'products/catalog/00215002-1.webp')
        self.assertEqual(result['matched'], 1)
        self.assertEqual(result['unmatched'], [])

    def test_matches_gallery_image_with_webp_extension(self):
        product = _make_product(product_code='125')
        self._drop_file('125-1.webp')
        self._drop_file('125-2.webp')

        sync_product_images()

        product.refresh_from_db()
        self.assertEqual(product.main_image.name, 'products/catalog/125-1.webp')
        gallery = ProductImage.objects.get(product=product, order=2)
        self.assertEqual(gallery.image.name, 'products/catalog/125-2.webp')

    def test_webp_extension_is_case_insensitive(self):
        product = _make_product(product_code='00215002')
        self._drop_file('00215002-1.WEBP')

        sync_product_images()

        product.refresh_from_db()
        self.assertEqual(product.main_image.name, 'products/catalog/00215002-1.WEBP')


class MainImageUrlPlaceholderTests(TestCase):
    """ Product.main_image_url: همیشه یک آدرس معتبر برمی‌گرداند - عکس واقعی یا placeholder. """

    def test_returns_main_image_url_when_present(self):
        product = _make_product()
        product.main_image = SimpleUploadedFile('placeholder-test.gif', TINY_GIF, content_type='image/gif')
        product.save(update_fields=['main_image'])
        self.addCleanup(product.main_image.delete, save=False)

        self.assertEqual(product.main_image_url, product.main_image.url)

    def test_returns_placeholder_static_url_when_main_image_is_empty(self):
        product = _make_product()
        self.assertFalse(product.main_image)

        self.assertEqual(product.main_image_url, static('theme/assets/images/Preload.webp'))


class HoverImageUrlPlaceholderTests(TestCase):
    """ Product.hover_image_url: برای جلوه‌ی Hover کارت محصول - عکس دوم گالری یا Preload-2.webp. """

    def test_returns_first_gallery_image_url_when_present(self):
        product = _make_product()
        gallery_image = ProductImage.objects.create(
            product=product, order=2,
            image=SimpleUploadedFile('hover-test.gif', TINY_GIF, content_type='image/gif'),
        )
        self.addCleanup(gallery_image.image.delete, save=False)

        self.assertEqual(product.hover_image_url, gallery_image.image.url)

    def test_returns_placeholder_static_url_when_gallery_is_empty(self):
        product = _make_product()
        self.assertFalse(product.gallery_images.exists())

        self.assertEqual(product.hover_image_url, static('theme/assets/images/Preload-2.webp'))
