from django.urls import path

from . import views

app_name = 'chat'

urlpatterns = [
    path('config/', views.config_view, name='config'),
]
