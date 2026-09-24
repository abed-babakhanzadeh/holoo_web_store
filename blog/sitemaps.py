"""نقشه‌ی سایت مقالات وبلاگ (SEO Phase B2) - در کنار مدل صاحبش، هم‌راستا با ProductSitemap."""

from django.contrib.sitemaps import Sitemap
from django.urls import reverse

from .models import Post


class BlogPostSitemap(Sitemap):
    changefreq = 'monthly'
    priority = 0.5

    def items(self):
        # Post.visible = status='published' و published_at گذشته؛ order_by صریح هم‌دلیل ProductSitemap
        return Post.visible.only('slug', 'updated_at').order_by('id')

    def lastmod(self, obj):
        return obj.updated_at

    def location(self, obj):
        return reverse('blog:detail', args=[obj.slug])
