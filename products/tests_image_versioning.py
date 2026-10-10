"""
نشانیِ نسخه‌دار تصاویر کاتالوگ (products/storage.py): جایگزینی عکسِ هم‌نام در پوشه‌ی کاتالوگ باید نشانیِ تصویر را عوض
کند، وگرنه مرورگر/CDN نسخه‌ی قدیمی را از کش نشان می‌دهد. MEDIA_ROOT هر تست موقت است؛ به media/ واقعی چیزی نوشته نمی‌شود.
"""
import itertools
import os
import shutil
import tempfile
from pathlib import Path

from django.test import TestCase, override_settings

from products.models import Product, ProductImage
from products.services import sync_product_images

_seq = itertools.count(1)


class VersionedImageUrlTests(TestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp(prefix='products-versioned-test-')
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=self.media_root)
        override.enable()
        self.addCleanup(override.disable)
        self.catalog = Path(self.media_root) / 'products' / 'catalog'
        self.catalog.mkdir(parents=True)
        n = next(_seq)
        self.product = Product.objects.create(
            name=f'کالای نسخه {n}', slug=f'versioned-img-{n}', erp_code=f'ERP-VER-{n}', product_code=f'9{n:04d}',
        )

    def _drop(self, name, mtime):
        path = self.catalog / name
        path.write_bytes(b'x' * (mtime % 50 + 1))
        os.utime(path, (mtime, mtime))
        return path

    def test_main_image_url_carries_file_mtime(self):
        code = self.product.product_code
        self._drop(f'{code}-1.jpg', 1_700_000_000)
        sync_product_images()
        self.product.refresh_from_db()
        self.assertTrue(self.product.main_image.url.endswith(f'/media/products/catalog/{code}-1.jpg?v=1700000000'))
        self.assertEqual(self.product.main_image_url, self.product.main_image.url)

    def test_same_name_replacement_changes_url_while_db_path_stays(self):
        """ هسته‌ی باگ: عکس هم‌نام عوض می‌شود، مسیر در دیتابیس همان می‌ماند، ولی نشانی باید تازه شود. """
        code = self.product.product_code
        self._drop(f'{code}-1.jpg', 1_700_000_000)
        sync_product_images()
        self.product.refresh_from_db()
        old_url, old_name = self.product.main_image.url, self.product.main_image.name

        self._drop(f'{code}-1.jpg', 1_700_005_000)
        result = sync_product_images()
        self.product.refresh_from_db()

        self.assertEqual(self.product.main_image.name, old_name)       # نام/مسیر ثابت
        self.assertEqual(result['updated'], 0)                         # sync چیزی برای نوشتن نداشت
        self.assertNotEqual(self.product.main_image.url, old_url)      # ولی نشانی تازه است
        self.assertTrue(self.product.main_image.url.endswith('?v=1700005000'))

    def test_gallery_and_hover_urls_are_versioned(self):
        code = self.product.product_code
        self._drop(f'{code}-1.jpg', 1_700_000_000)
        self._drop(f'{code}-2.jpg', 1_700_000_777)
        sync_product_images()
        gallery = ProductImage.objects.get(product=self.product, order=2)
        self.assertTrue(gallery.image.url.endswith('?v=1700000777'))
        self.assertTrue(self.product.hover_image_url.endswith('?v=1700000777'))

    def test_missing_file_gives_plain_url_without_error(self):
        Product.objects.filter(pk=self.product.pk).update(main_image='products/catalog/ghost-1.jpg')
        self.product.refresh_from_db()
        self.assertEqual(self.product.main_image.url, '/media/products/catalog/ghost-1.jpg')
