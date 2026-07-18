from django.urls import path
from . import views

app_name = 'recently_viewed'
urlpatterns = [
    path('remove/<int:product_id>/', views.RemoveRecentlyViewedView.as_view(), name='remove'),
    path('clear/', views.ClearRecentlyViewedView.as_view(), name='clear'),
    path('', views.RecentlyViewedListView.as_view(), name='list'),
]
