from django.urls import path

from . import views

app_name = 'locations'

urlpatterns = [
    path('cities/', views.city_options, name='city_options'),
    path('zone-field/', views.zone_field, name='zone_field'),
]
