from django.urls import path
from . import views

app_name = 'products'
urlpatterns = [
    # صفحه اصلی فروشگاه
    path('', views.ProductListView.as_view(), name='product_list'),
    
    # تغییر <slug:slug> به <str:slug> برای پشتیبانی کامل از حروف فارسی
    path('product/<str:slug>/', views.ProductDetailView.as_view(), name='product_detail'),
]