from django.urls import path
from . import views

app_name = 'orders'
urlpatterns = [
    path('checkout/', views.CheckoutView.as_view(), name='checkout'),
    path('update-invoice/', views.UpdateInvoiceView.as_view(), name='update_invoice'),
    path('submit/', views.SubmitOrderView.as_view(), name='submit_order'),
    path('success/<int:order_id>/', views.OrderSuccessView.as_view(), name='order_success'),
    path('checkout-cart/<int:product_id>/<str:action>/', views.CheckoutCartUpdateView.as_view(), name='checkout_cart_update'),
    path('history/', views.UserOrderHistoryView.as_view(), name='order_history'),
    path('history/<int:order_id>/', views.OrderDetailView.as_view(), name='order_detail'),
    path('history/<int:order_id>/detail/', views.OrderFullDetailView.as_view(), name='order_detail_full'),
]