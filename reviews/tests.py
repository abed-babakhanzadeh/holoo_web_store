"""تست نظرات — تفکیک از اپ سفارش، ترتیب واحد، و حفظ کش prefetch."""

from django.test import TestCase

from accounts.models import CustomUser
from orders.models import Order, OrderItem
from payments.models import Transaction
from products.models import Category, Product, ProductColor
from reviews.constants import review_order_by, REVIEW_SORT_OPTIONS
from reviews.models import Review, ReviewPoint
from reviews.purchases import purchase_info
from reviews.templatetags.review_tags import has_kind, star_range


class SortConstantTests(TestCase):
    def test_single_definition_is_shared_by_both_views(self):
        """ قبلاً این دیکشنری در products/views.py و reviews/views.py جدا تعریف شده بود """
        import products.views as product_views
        import reviews.views as review_views

        self.assertFalse(hasattr(product_views, 'REVIEW_SORT_OPTIONS'))
        self.assertIs(review_views.review_order_by, review_order_by)

    def test_invalid_sort_falls_back_to_newest(self):
        self.assertEqual(review_order_by('nonsense'), REVIEW_SORT_OPTIONS['newest'])


class VerifiedPurchaseTests(TestCase):
    """ reviews دیگر orders را import نمی‌کند؛ اطلاعات خرید از رجیستری می‌آید """

    @classmethod
    def setUpTestData(cls):
        cls.user = CustomUser.objects.create_user(phone_number='09120000060')
        category = Category.objects.create(name='تست', slug='rev-cat')
        cls.product = Product.objects.create(
            name='کالا', slug='rev-product', erp_code='ERP-R-1',
            category=category, price=50000, stock=3,
        )
        cls.color = ProductColor.objects.create(product=cls.product, name='قرمز', hex_code='#f00')

    def test_reviews_app_does_not_import_orders(self):
        """ بررسی ساختاری (نه متنی): هیچ ماژول reviews نباید اپ orders را import کند """
        import ast
        import pathlib
        import reviews

        offenders = []
        for path in pathlib.Path(reviews.__path__[0]).rglob('*.py'):
            if path.name == 'tests.py':
                continue
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or '').split('.')[0] == 'orders':
                    offenders.append(f'{path.name}:{node.lineno}')
                elif isinstance(node, ast.Import):
                    if any(a.name.split('.')[0] == 'orders' for a in node.names):
                        offenders.append(f'{path.name}:{node.lineno}')

        self.assertEqual(offenders, [], f'وابستگی مستقیم به orders: {offenders}')

    def test_user_without_purchase_is_not_verified(self):
        purchased, color = purchase_info(self.user, self.product)
        self.assertFalse(purchased)
        self.assertIsNone(color)

    def test_unpaid_order_does_not_count_as_purchase(self):
        order = Order.objects.create(
            user=self.user, first_name='ع', last_name='ر', phone='09120000060',
            address='تهران', payment_method='cash', shipping_cost=0, total_price=50000,
        )
        OrderItem.objects.create(order=order, product=self.product, price=50000, quantity=1)
        self.assertFalse(purchase_info(self.user, self.product)[0])

    def test_paid_order_marks_verified_and_returns_color(self):
        order = Order.objects.create(
            user=self.user, first_name='ع', last_name='ر', phone='09120000060',
            address='تهران', payment_method='cash', shipping_cost=0, total_price=50000,
        )
        OrderItem.objects.create(order=order, product=self.product, color=self.color,
                                 price=50000, quantity=1)
        Transaction.objects.create(user=self.user, order=order, amount=50000,
                                   authority='A-REV-1', status='success')

        purchased, color = purchase_info(self.user, self.product)
        self.assertTrue(purchased)
        self.assertEqual(color, self.color)

    def test_publishing_a_review_stamps_verified_purchase(self):
        order = Order.objects.create(
            user=self.user, first_name='ع', last_name='ر', phone='09120000060',
            address='تهران', payment_method='cash', shipping_cost=0, total_price=50000,
        )
        OrderItem.objects.create(order=order, product=self.product, price=50000, quantity=1)
        Transaction.objects.create(user=self.user, order=order, amount=50000,
                                   authority='A-REV-2', status='success')

        review = Review.objects.create(product=self.product, user=self.user, rating=5,
                                       body='خوب بود', status='pending')
        self.assertFalse(review.is_verified_purchase)

        review.status = 'published'
        review.save()
        review.refresh_from_db()
        self.assertTrue(review.is_verified_purchase)


class TemplateTagTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        user = CustomUser.objects.create_user(phone_number='09120000061')
        category = Category.objects.create(name='تست', slug='tag-cat')
        product = Product.objects.create(name='کالا', slug='tag-product', erp_code='ERP-T-1',
                                         category=category, price=1000, stock=1)
        cls.review = Review.objects.create(product=product, user=user, rating=4,
                                           body='متن', status='published')
        ReviewPoint.objects.create(review=cls.review, kind='pro', text='سبک')
        ReviewPoint.objects.create(review=cls.review, kind='con', text='گران')

    def test_has_kind_detects_both_kinds(self):
        points = list(self.review.points.all())
        self.assertTrue(has_kind(points, 'pro'))
        self.assertTrue(has_kind(points, 'con'))
        self.assertFalse(has_kind(points, 'other'))

    def test_has_kind_uses_prefetch_cache_instead_of_new_query(self):
        """
        رگرسیون: has_kind قبلاً روی ورودی دوباره .all() می‌زد و چون .all() روی یک
        QuerySet ارزیابی‌شده کوئری‌ست تازه‌ی بدون کش می‌سازد، برای هر نظر یک کوئری
        اضافه شلیک می‌شد.
        """
        review = Review.objects.prefetch_related('points').get(pk=self.review.pk)
        points = review.points.all()
        list(points)  # اجرای prefetch

        with self.assertNumQueries(0):
            has_kind(points, 'pro')
            has_kind(points, 'con')

    def test_star_range(self):
        self.assertEqual(star_range(3), [True, True, True, False, False])
        self.assertEqual(star_range(None), [False] * 5)
        self.assertEqual(star_range('bad'), [False] * 5)
