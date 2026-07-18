from django.urls import path
from . import views

app_name = 'cart'
urlpatterns = [
    path('add/<int:product_id>/', views.AddToCartView.as_view(), name='add_to_cart'),
    path('decrease/<int:product_id>/', views.DecreaseCartView.as_view(), name='decrease_cart'),
    path('remove/<int:product_id>/', views.RemoveFromCartView.as_view(), name='remove_from_cart'),
    path('status/<int:product_id>/', views.CartButtonStatusView.as_view(), name='cart_button_status'),
    path('mini-cart/', views.MiniCartView.as_view(), name='mini_cart'),
    path('nav/', views.NavCartView.as_view(), name='nav_cart'),
]