from django.urls import path

from . import guide

app_name = 'guide'
urlpatterns = [
    path('how-to-order/', guide.HowToOrderView.as_view(), name='order'),
    path('shipping/', guide.ShippingView.as_view(), name='shipping'),
    path('payment/', guide.PaymentView.as_view(), name='payment'),
]
