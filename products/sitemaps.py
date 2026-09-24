"""
نقشه‌ی سایت محصولات/دسته‌ها/صفحات ایستا (SEO Phase B2).

فقط URLهای کانونیکالِ تصمیم‌گرفته‌شده در Phase B1 اینجا فهرست می‌شوند: محصولات واقعاً نمایش‌داده‌شدنی،
فقط دسته‌های سطح‌اول (همان‌هایی که /category/<slug>/ برایشان معتبر است - زیردسته‌ها URL کانونیکال
مستقل ندارند)، و چند صفحه‌ی ایستای مهم.
"""

from django.contrib.sitemaps import Sitemap
from django.urls import reverse

from .models import Category, Product


class ProductSitemap(Sitemap):
    changefreq = 'weekly'
    priority = 0.8

    def items(self):
        # order_by صریح لازم است: بدون آن روی بک‌اند MSSQL ترتیب صفحه‌بندی داخلی sitemap
        # framework (وقتی تعداد از سقف ۵۰هزار بگذرد) تضمین‌شده نیست - همان دلیلی که
        # ProductListView هم برای Paginator خودش order_by صریح دارد.
        return Product.visible.only('slug', 'updated_at').order_by('id')

    def lastmod(self, obj):
        return obj.updated_at

    def location(self, obj):
        return reverse('products:product_detail', args=[obj.slug])


class CategorySitemap(Sitemap):
    changefreq = 'weekly'
    priority = 0.6

    def items(self):
        # parent__isnull=True دقیقاً همان شرطی است که CategoryDetailView هم برای معتبر
        # بودن URL چک می‌کند؛ زیردسته‌ها URL کانونیکال مستقل ندارند (تصمیم Phase B1)
        # و عمداً اینجا نیستند. Category فیلد updated_at/created_at ندارد، پس lastmod تعریف نمی‌شود.
        return Category.objects.filter(is_active=True, parent__isnull=True).order_by('id')

    def location(self, obj):
        return reverse('products:category_detail', args=[obj.slug])


class StaticViewSitemap(Sitemap):
    priority = 1.0
    changefreq = 'daily'

    def items(self):
        return ['products:home', 'products:product_list', 'blog:list']

    def location(self, item):
        return reverse(item)
