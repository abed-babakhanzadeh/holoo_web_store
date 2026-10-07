from django.urls import path

from . import api, views

app_name = 'chat'

urlpatterns = [
    path('config/', views.config_view, name='config'),
    path('state/', api.state_view, name='state'),
    path('conversations/', api.create_view, name='create'),
    path('c/<uuid:public_id>/messages/', api.messages_view, name='messages'),
    path('c/<uuid:public_id>/send/', api.send_view, name='send'),
    path('c/<uuid:public_id>/read/', api.read_view, name='read'),
    path('c/<uuid:public_id>/close/', api.close_view, name='close'),
    path('c/<uuid:public_id>/typing/', api.typing_view, name='typing'),
]
