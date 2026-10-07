"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.contrib.sitemaps.views import sitemap
from django.urls import path, include
from django.views.generic import RedirectView, TemplateView
from django.conf.urls.static import static
from config import settings
from blog.sitemaps import BlogPostSitemap
from products.sitemaps import CategorySitemap, ProductSitemap, StaticViewSitemap

# SEO Phase B2: فقط URLهای کانونیکالِ تصمیم‌گرفته‌شده در Phase B1 (نگاه کنید products/sitemaps.py)
sitemaps = {
    'static': StaticViewSitemap,
    'products': ProductSitemap,
    'categories': CategorySitemap,
    'blog': BlogPostSitemap,
}

urlpatterns = [
    path('admin/', admin.site.urls),
    path('robots.txt', TemplateView.as_view(template_name='robots.txt', content_type='text/plain; charset=utf-8')),
    path('sitemap.xml', sitemap, {'sitemaps': sitemaps}, name='sitemap'),
    path('accounts/', include('accounts.urls')),
    path('locations/', include('locations.urls')),
    path('cart/', include('cart.urls')),
    path('orders/', include('orders.urls')),
    path('my-discounts/', include('promotions.urls')),
    path('payments/', include('payments.urls')),
    path('wallet/', include('wallet.urls')),
    path('loyalty/', include('loyalty.urls')),
    path('returns/', include('returns.urls')),
    path('wishlist/', include('wishlist.urls')),
    path('recently-viewed/', include('recently_viewed.urls')),
    path('reviews/', include('reviews.urls')),
    path('compare/', include('compare.urls')),
    path('blog/', include('blog.urls')),
    path('ckeditor5/', include('django_ckeditor_5.urls')),
    path('chat/', include('chat.urls')),
    path('', include('products.urls')),
]

# اضافه کردن روتِ نمایش فایل‌های مدیا فقط در محیط برنامه‌نویسی (Local)
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    