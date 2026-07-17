from django.urls import path
from . import views

app_name = 'cart'
urlpatterns = [
    path('add/<int:product_id>/', views.AddToCartView.as_view(), name='add_to_cart'),
    path('decrease/<int:product_id>/', views.DecreaseCartView.as_view(), name='decrease_cart'),
    path('mini-cart/', views.MiniCartView.as_view(), name='mini_cart'),
    path('nav/', views.NavCartView.as_view(), name='nav_cart'),
]