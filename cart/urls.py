from django.urls import path
from . import views

app_name = 'cart'
urlpatterns = [
    path('add/<int:product_id>/', views.add_to_cart, name='add_to_cart'),
    path('decrease/<int:product_id>/', views.decrease_cart, name='decrease_cart'),
    path('mini-cart/', views.mini_cart, name='mini_cart'),
]