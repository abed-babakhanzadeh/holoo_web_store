from django.urls import path
from . import views

app_name = 'wishlist'
urlpatterns = [
    path('toggle/<int:product_id>/', views.ToggleFavoriteView.as_view(), name='toggle_favorite'),
    path('status/<int:product_id>/', views.FavoriteStatusView.as_view(), name='favorite_status'),
    path('remove/<int:product_id>/', views.RemoveFromWishlistView.as_view(), name='remove_favorite'),
    path('', views.WishlistListView.as_view(), name='list'),
]
