from django.urls import path

from . import views

app_name = 'promotions'

urlpatterns = [
    path('', views.MyCouponsView.as_view(), name='my_codes'),
    path('get/', views.GetCouponView.as_view(), name='get_code'),
    path('get/<int:pk>/claim/', views.ClaimCouponView.as_view(), name='claim'),
]
