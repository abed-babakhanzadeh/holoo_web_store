import random
from datetime import timedelta
from django.utils import timezone
from django.shortcuts import render, redirect
from django.contrib.auth import login, logout
from django.views import View  # ایمپورت کلاس پایه ویوها
from django.http import HttpResponse
from .models import CustomUser, OTPRequest, OTPPurpose, normalize_phone_number, UserStatus
from services.sms import send_otp_sms
from django.contrib.auth.mixins import LoginRequiredMixin # برای اجباری کردن لاگین
from holoo.tasks import sync_user_to_holoo

class LoginView(View):
    """ کلاس مدیریت صفحه اصلی لاگین """
    template_name = 'accounts/login.html'

    def get(self, request, *args, **kwargs):
        # اگر کاربر قبلاً لاگین کرده بود، به صفحه اصلی برود
        if request.user.is_authenticated:
            return redirect('/') 
            
        return render(request, self.template_name)


class SendOTPView(View):
    """ کلاس دریافت شماره موبایل با HTMX، تولید و ارسال کد """
    phone_template = 'accounts/partials/phone_form.html'
    otp_template = 'accounts/partials/otp_form.html'

    def post(self, request, *args, **kwargs):
        raw_phone = request.POST.get('phone_number')
        
        try:
            # 1. نرمال‌سازی شماره موبایل
            phone_number = normalize_phone_number(raw_phone)
            
            # 2. تولید کد 6 رقمی تصادفی
            code = str(random.randint(100000, 999999))
            
            # 3. ذخیره در دیتابیس با انقضای 2 دقیقه‌ای
            expires_at = timezone.now() + timedelta(minutes=2)
            client_ip = request.META.get('REMOTE_ADDR') 
            
            OTPRequest.objects.create(
                phone_number=phone_number,
                code=code,
                purpose=OTPPurpose.REGISTER_LOGIN,
                ip_address=client_ip,
                expires_at=expires_at
            )
            
            # 4. فراخوانی سرویس پیامک مجازی
            send_otp_sms(phone_number, code)
            
            # 5. برگرداندن فرم دوم
            return render(request, self.otp_template, {'phone_number': phone_number})
            
        except ValueError as e:
            # در صورت خطای اعتبارسنجی
            return render(request, self.phone_template, {'error': str(e)})


class VerifyOTPView(View):
    """ کلاس دریافت کد تایید با HTMX، بررسی صحت آن و لاگین کاربر """
    otp_template = 'accounts/partials/otp_form.html'

    def post(self, request, *args, **kwargs):
        phone_number = request.POST.get('phone_number')
        code = request.POST.get('code')
        
        # فراخوانی متد هوشمند مدل برای بررسی صحت و انقضا
        is_valid, error_message = OTPRequest.verify_code(phone_number, code)
        
        if not is_valid:
            # فقط پیام خطا به صورت HTML برگردانده می‌شود تا داخل کانتینر خطا لود شود 👇
            # این کار باعث می‌شود فرم و اسکریپت تایمر اصلاً دست نخورند و ریست نشوند
            return HttpResponse(f'<p class="text-red-500 text-xs italic">{error_message}</p>')

        # اگر کد درست بود، پیدا کردن یا ساختن کاربر
        user, created = CustomUser.objects.get_or_create(
            phone_number=phone_number,
            defaults={'status': UserStatus.PENDING_PROFILE} # تغییر به PENDING_PROFILE
        )
        
        login(request, user)
        
        # ریدایرکت کل صفحه با هدر HTMX به روت اصلی سایت
        response = HttpResponse() 
        response['HX-Redirect'] = '/' 
        return response
    
class LogoutView(View):
    """ کلاس خروج از حساب کاربری """
    def post(self, request, *args, **kwargs):
        logout(request)
        # پس از خروج، کاربر را به صفحه اصلی ریدایرکت می‌کنیم
        response = HttpResponse()
        response['HX-Redirect'] = '/'
        return response
    
class ProfileCompleteView(LoginRequiredMixin, View):
    """ کلاس مدیریت فرم تکمیل اطلاعات پروفایل کاربر """
    template_name = 'accounts/profile_complete.html'
    partial_template = 'accounts/partials/profile_form.html'

    def get(self, request, *args, **kwargs):
        # اگر کاربر با ویژگی‌های درخواستی از قبل کامل شده بود، برگردد به صفحه اصلی
        if request.user.status == UserStatus.ACTIVE:
            return redirect('/')
            
        # بررسی اینکه آیا درخواست از سمت HTMX آمده یا رفرش مستقیم صفحه است
        if request.headers.get('HX-Request'):
            return render(request, self.partial_template)
        return render(request, self.template_name)

    def post(self, request, *args, **kwargs):
        user = request.user
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        national_code = request.POST.get('national_code', '').strip()

        # نگهداری مقادیر برای برگشت به فرم در صورت خطا
        context = {
            'first_name': first_name,
            'last_name': last_name,
            'national_code': national_code
        }

        # ۱. اعتبارسنجی فیلدهای الزامی
        if not first_name or not last_name or not national_code:
            context['error'] = 'تکمیل تمامی فیلدها الزامی است.'
            return render(request, self.partial_template, context)

        # ۲. بررسی فرمت کد ملی (۱۰ رقم عددی)
        if not national_code.isdigit() or len(national_code) != 10:
            context['error'] = 'کد ملی وارد شده معتبر نیست (باید ۱۰ رقم باشد).'
            return render(request, self.partial_template, context)

    # ۳. ذخیره در دیتابیس سایت با وضعیت در انتظار هلو
        user.first_name = first_name
        user.last_name = last_name
        user.national_code = national_code
        user.status = UserStatus.PENDING_ERP_SYNC
        user.save()

        # ۴. پرتاب کردن تسک به سمت Celery در پس‌زمینه (Async)
        # متد delay باعث می‌شود جنگو معطل نماند و فقط کار را به صف Redis بفرستد
        sync_user_to_holoo.delay(user.id)

        # ۵. ریدایرکت کاربر به صفحه اصلی
        response = HttpResponse()
        response['HX-Redirect'] = '/'
        return response