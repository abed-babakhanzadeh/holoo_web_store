"""
تست‌های products/services.py::delete_products_safely.

سناریوی اصلی: مدل reviews.Review یک FK خودارجاع دارد (parent، برای پاسخ‌ها) با on_delete=CASCADE.
حذف دسته‌ای محصولاتی که رشته‌ی نظر/پاسخِ چندلایه دارند، اگر مستقیم با QuerySet.delete() جنگو
انجام شود، Collector جنگو پیش از حذف واقعی ستون parent_id همه‌ی آن ردیف‌ها را یک‌جا Null می‌کند
(مکانیزم دفاعی خودِ جنگو برای مدل‌های خودارجاع) که می‌تواند موقتاً چند ردیف هم‌زمان parent=NULL
برای یک (user, product) بسازد و با ایندکس یکتای one_top_level_review_per_user_product تداخل کند.
"""
import itertools

from django.test import TestCase

from accounts.testing import make_approved_user
from products.models import Category, Product
from products.services import delete_products_safely
from reviews.models import Review

_seq = itertools.count(1)


def _make_product(is_active=False):
    n = next(_seq)
    category = Category.objects.create(name=f'دسته تست حذف {n}', slug=f'safe-delete-cat-{n}')
    return Product.objects.create(
        name=f'کالای تست حذف {n}', slug=f'safe-delete-product-{n}', erp_code=f'ERP-SAFE-DELETE-{n}',
        category=category, price=10000, stock=0, is_active=is_active,
    )


class DeleteProductsSafelyTests(TestCase):
    def test_deletes_products_with_no_reviews(self):
        p1 = _make_product()
        p2 = _make_product()

        deleted_products, deleted_reviews = delete_products_safely(Product.objects.filter(id__in=[p1.id, p2.id]))

        self.assertEqual(deleted_products, 2)
        self.assertEqual(deleted_reviews, 0)
        self.assertFalse(Product.objects.filter(id__in=[p1.id, p2.id]).exists())

    def test_deletes_product_with_multi_level_reply_thread_without_crashing(self):
        """ همان سناریوی واقعی که با QuerySet.delete() مستقیم به IntegrityError می‌خورد:
        نظر اصلی -> پاسخ -> دو پاسخِ پاسخ، همه از یک کاربر روی یک محصول. """
        product = _make_product()
        user = make_approved_user('09120000900', price_level=1)

        top = Review.objects.create(product=product, user=user, rating=5, body='نظر اصلی', status='published')
        reply = Review.objects.create(product=product, user=user, parent=top, body='پاسخ', status='published')
        Review.objects.create(product=product, user=user, parent=reply, body='پاسخِ پاسخ ۱', status='published')
        Review.objects.create(product=product, user=user, parent=reply, body='پاسخِ پاسخ ۲', status='published')

        deleted_products, deleted_reviews = delete_products_safely(Product.objects.filter(id=product.id))

        self.assertEqual(deleted_products, 1)
        self.assertEqual(deleted_reviews, 4)
        self.assertFalse(Product.objects.filter(id=product.id).exists())
        self.assertFalse(Review.objects.filter(product_id=product.id).exists())

    def test_reviews_on_other_products_are_untouched(self):
        target = _make_product()
        other = _make_product()
        user = make_approved_user('09120000901', price_level=1)
        Review.objects.create(product=target, user=user, rating=5, body='حذف شود', status='published')
        kept = Review.objects.create(product=other, user=user, rating=4, body='بماند', status='published')

        delete_products_safely(Product.objects.filter(id=target.id))

        self.assertTrue(Review.objects.filter(id=kept.id).exists())
        self.assertTrue(Product.objects.filter(id=other.id).exists())

    def test_empty_queryset_is_a_no_op(self):
        deleted_products, deleted_reviews = delete_products_safely(Product.objects.none())
        self.assertEqual((deleted_products, deleted_reviews), (0, 0))
