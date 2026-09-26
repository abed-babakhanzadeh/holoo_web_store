from django.urls import path

from . import views

app_name = 'wallet'
urlpatterns = [
    path('', views.WalletDashboardView.as_view(), name='dashboard'),
    path('topup/', views.WalletTopupView.as_view(), name='topup'),
    path('topup/gateway/<str:authority>/', views.WalletMockGatewayView.as_view(), name='mock_gateway'),
    path('topup/callback/', views.WalletTopupCallbackView.as_view(), name='topup_callback'),
    path('withdraw/', views.WalletWithdrawView.as_view(), name='withdraw'),
]
