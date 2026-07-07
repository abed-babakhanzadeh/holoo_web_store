from django.urls import path
from . import views

app_name = 'accounts'

urlpatterns = [
    # توجه: متد as_view() برای کلاس‌بیس‌ها اضافه شد
    path('login/', views.LoginView.as_view(), name='login_view'),
    path('send-otp/', views.SendOTPView.as_view(), name='send_otp'),
    path('verify-otp/', views.VerifyOTPView.as_view(), name='verify_otp'),
    path('logout/', views.LogoutView.as_view(), name='logout'),
]