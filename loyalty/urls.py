from django.urls import path

from . import views

app_name = 'loyalty'
urlpatterns = [
    path('', views.LoyaltyDashboardView.as_view(), name='dashboard'),
    path('rewards/', views.RewardCatalogView.as_view(), name='rewards'),
    path('redeem/wallet/', views.RedeemToWalletView.as_view(), name='redeem_wallet'),
    path('rewards/<int:pk>/redeem/', views.RedeemRewardView.as_view(), name='redeem_reward'),
]
