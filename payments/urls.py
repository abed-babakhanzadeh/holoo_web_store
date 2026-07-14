from django.urls import path
from . import views

app_name = 'payments'
urlpatterns = [
    path('start/<int:order_id>/', views.PaymentStartView.as_view(), name='start_payment'),
    path('gateway/<str:authority>/', views.MockGatewayView.as_view(), name='mock_gateway'),
    path('callback/', views.PaymentCallbackView.as_view(), name='callback'),
]
