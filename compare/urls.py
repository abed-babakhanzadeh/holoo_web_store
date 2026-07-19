from django.urls import path
from . import views

app_name = 'compare'
urlpatterns = [
    path('toggle/<int:product_id>/', views.CompareToggleView.as_view(), name='toggle_compare'),
    path('status/<int:product_id>/', views.CompareStatusView.as_view(), name='compare_status'),
    path('remove/<int:product_id>/', views.CompareRemoveView.as_view(), name='remove_compare'),
    path('clear/', views.CompareClearView.as_view(), name='clear_compare'),
    path('add/<int:product_id>/', views.CompareAddView.as_view(), name='add_compare'),
    path('search/', views.CompareSearchView.as_view(), name='search'),
    path('badge/', views.CompareBadgeView.as_view(), name='badge'),
    path('', views.CompareListView.as_view(), name='list'),
]
