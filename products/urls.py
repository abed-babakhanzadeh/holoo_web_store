from django.urls import path
from . import views

app_name = 'products'
urlpatterns = [
    # صفحه اصلی فروشگاه (ویترین)
    path('', views.HomeView.as_view(), name='home'),

    # صفحه فروشگاه (لیست کامل محصولات با فیلتر/جستجو)
    path('shop/', views.ProductListView.as_view(), name='product_list'),

    # تغییر <slug:slug> به <str:slug> برای پشتیبانی کامل از حروف فارسی
    path('product/<str:slug>/', views.ProductDetailView.as_view(), name='product_detail'),
]