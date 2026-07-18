from django.urls import path
from . import views

app_name = 'accounts'

urlpatterns = [
    # توجه: متد as_view() برای کلاس‌بیس‌ها اضافه شد
    path('login/', views.LoginView.as_view(), name='login_view'),
    path('phone-form/', views.PhoneFormView.as_view(), name='phone_form'),
    path('send-otp/', views.SendOTPView.as_view(), name='send_otp'),
    path('verify-otp/', views.VerifyOTPView.as_view(), name='verify_otp'),
    path('logout/', views.LogoutView.as_view(), name='logout'),
    path('profile/complete/', views.ProfileCompleteView.as_view(), name='profile_complete'),
    path('dashboard/', views.DashboardView.as_view(), name='dashboard'),
    path('profile/', views.ProfileView.as_view(), name='profile'),
    path('profile/avatar/', views.ProfileAvatarUploadView.as_view(), name='profile_avatar'),
    path('wallet/', views.WalletView.as_view(), name='wallet'),

    # بخش‌هایی از قالب که هنوز بک‌اند واقعی ندارند (placeholder موقت)
    path('soon/tickets/', views.ComingSoonView.as_view(section_title='تیکت‌های پشتیبانی', active_nav='tickets'), name='soon_tickets'),
    path('soon/comments/', views.ComingSoonView.as_view(section_title='دیدگاه‌های من', active_nav='comments'), name='soon_comments'),
    path('soon/discounts/', views.ComingSoonView.as_view(section_title='تخفیف‌ها و کارت هدیه', active_nav='discounts'), name='soon_discounts'),
    path('soon/notifications/', views.ComingSoonView.as_view(section_title='اعلان‌های سایت', active_nav='notifications'), name='soon_notifications'),
    path('soon/wallet-topup/', views.ComingSoonView.as_view(section_title='افزایش موجودی کیف پول', active_nav='wallet'), name='soon_wallet_topup'),
    path('soon/wallet-transfer/', views.ComingSoonView.as_view(section_title='انتقال وجه', active_nav='wallet'), name='soon_wallet_transfer'),
]