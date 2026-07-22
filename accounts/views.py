import random
import json
import secrets
import jdatetime
import requests
from datetime import timedelta, datetime
from urllib.parse import urlencode
from django.utils import timezone
from django.shortcuts import render, redirect
from django.urls import reverse
from django.conf import settings
from django.contrib.auth import login, logout, authenticate, update_session_auth_hash
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.views import View  # ایمپورت کلاس پایه ویوها
from django.views.generic import TemplateView
from django.http import HttpResponse
from .models import CustomUser, OTPRequest, OTPPurpose, normalize_phone_number, UserStatus
from services.sms import send_otp_sms
from django.contrib.auth.mixins import LoginRequiredMixin # برای اجباری کردن لاگین
from holoo.tasks import sync_user_to_holoo
from orders.models import Order
from payments.models import Transaction
from wishlist.models import FavoriteProduct
from recently_viewed.models import RecentlyViewed

class LoginView(View):
    """ کلاس مدیریت صفحه اصلی لاگین """
    template_name = 'accounts/login.html'

    def get(self, request, *args, **kwargs):
        # اگر کاربر قبلاً لاگین کرده بود، به صفحه اصلی برود
        if request.user.is_authenticated:
            return redirect('/') 
            
        return render(request, self.template_name)


class PhoneFormView(View):
    """ بازگرداندن مرحله‌ی اول (شماره موبایل) بدون رفرش کل صفحه/مودال """
    template_name = 'accounts/partials/phone_step.html'

    def get(self, request, *args, **kwargs):
        return render(request, self.template_name)


class LoginTabsView(View):
    """ بازگرداندن کل تب‌بندی ورود (رمز عبور / پیامک) - برای بازگشت از مسیر فراموشی رمز """
    template_name = 'accounts/partials/login_tabs.html'

    def get(self, request, *args, **kwargs):
        return render(request, self.template_name)


class SendOTPView(View):
    """ کلاس دریافت شماره موبایل با HTMX، تولید و ارسال کد """
    phone_template = 'accounts/partials/phone_step.html'
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
        if created:
            # تا has_usable_password/has_real_password درست تشخیص بدهند که هنوز رمزی تعیین نشده
            # (رمز واقعی در گام تکمیل پروفایل تعیین می‌شود)
            user.set_unusable_password()
            user.save(update_fields=['password'])

        login(request, user)
        
        # ریدایرکت کل صفحه با هدر HTMX به روت اصلی سایت
        response = HttpResponse() 
        response['HX-Redirect'] = '/' 
        return response
    
class LoginWithPasswordView(View):
    """
    ورود با شماره موبایل + رمز عبور (تب دوم پاپ‌آپ ورود).
    برخلاف مراحل OTP (که کل .auth-step-container را جایگزین می‌کنند)، این ویو فقط یک قطعه‌ی
    خطا برمی‌گرداند (هدف hx-post روی #password-error-container است) تا با ورود اشتباه، نوار تب‌ها
    از بین نرود و کاربر همان‌جا دوباره تلاش کند.
    """

    def post(self, request, *args, **kwargs):
        raw_phone = request.POST.get('phone_number', '')
        password = request.POST.get('password', '')

        try:
            phone_number = normalize_phone_number(raw_phone)
        except ValueError as e:
            return HttpResponse(f'<p class="text-red-500 text-xs italic">{e}</p>')

        user = CustomUser.objects.filter(phone_number=phone_number).first()
        if user and not user.has_real_password():
            return HttpResponse(
                '<p class="text-red-500 text-xs italic">برای این شماره هنوز رمز عبوری تعیین نشده. '
                'از تب «ورود با پیامک» استفاده کنید یا از بخش «رمز عبور را فراموش کرده‌اید» یک رمز تعیین کنید.</p>'
            )

        authenticated_user = authenticate(request, username=phone_number, password=password)
        if authenticated_user is None:
            return HttpResponse('<p class="text-red-500 text-xs italic">شماره موبایل یا رمز عبور اشتباه است.</p>')

        login(request, authenticated_user)
        response = HttpResponse()
        response['HX-Redirect'] = '/'
        return response


class ForgotPasswordSendOTPView(View):
    """ گام اول بازیابی رمز عبور: گرفتن شماره موبایل و ارسال کد یکبارمصرف """
    phone_template = 'accounts/partials/forgot_password_phone.html'
    otp_template = 'accounts/partials/forgot_password_otp.html'

    def get(self, request, *args, **kwargs):
        """ نمایش فرم اولیه‌ی «فراموشی رمز» (لینک از تب ورود با رمز عبور) """
        return render(request, self.phone_template)

    def post(self, request, *args, **kwargs):
        raw_phone = request.POST.get('phone_number')

        try:
            phone_number = normalize_phone_number(raw_phone)
        except ValueError as e:
            return render(request, self.phone_template, {'error': str(e)})

        if not CustomUser.objects.filter(phone_number=phone_number).exists():
            return render(request, self.phone_template, {'error': 'حسابی با این شماره موبایل پیدا نشد.'})

        code = str(random.randint(100000, 999999))
        expires_at = timezone.now() + timedelta(minutes=2)
        client_ip = request.META.get('REMOTE_ADDR')

        OTPRequest.objects.create(
            phone_number=phone_number,
            code=code,
            purpose=OTPPurpose.RESET_PASSWORD,
            ip_address=client_ip,
            expires_at=expires_at
        )
        send_otp_sms(phone_number, code)

        return render(request, self.otp_template, {'phone_number': phone_number})


class ForgotPasswordVerifyView(View):
    """ گام دوم بازیابی رمز عبور: بررسی کد و در صورت صحت، نمایش فرم تعیین رمز جدید """
    otp_template = 'accounts/partials/forgot_password_otp.html'
    set_template = 'accounts/partials/forgot_password_set.html'

    def post(self, request, *args, **kwargs):
        phone_number = request.POST.get('phone_number')
        code = request.POST.get('code')

        is_valid, error_message = OTPRequest.verify_code(phone_number, code, purpose=OTPPurpose.RESET_PASSWORD)
        if not is_valid:
            return HttpResponse(f'<p class="text-red-500 text-xs italic">{error_message}</p>')

        # تایید هویت با موبایل کامل نشد؛ فقط شماره‌ی تاییدشده در سشن نگه داشته می‌شود تا گام بعد
        # (تعیین رمز جدید) از سمت کاربر قابل دستکاری نباشد (به‌جای اعتماد به فیلد مخفی فرم)
        request.session['reset_password_phone'] = phone_number
        request.session['reset_password_verified_at'] = timezone.now().isoformat()

        return render(request, self.set_template, {'phone_number': phone_number})


class ForgotPasswordSetView(View):
    """ گام سوم بازیابی رمز عبور: ثبت رمز عبور جدید برای شماره‌ی تاییدشده در سشن """
    set_template = 'accounts/partials/forgot_password_set.html'

    def post(self, request, *args, **kwargs):
        phone_number = request.session.get('reset_password_phone')
        verified_at_raw = request.session.get('reset_password_verified_at')

        expired = True
        if verified_at_raw:
            verified_at = datetime.fromisoformat(verified_at_raw)
            if timezone.is_naive(verified_at):
                verified_at = timezone.make_aware(verified_at)
            expired = timezone.now() - verified_at > timedelta(minutes=10)

        if not phone_number or expired:
            request.session.pop('reset_password_phone', None)
            request.session.pop('reset_password_verified_at', None)
            return render(request, 'accounts/partials/forgot_password_phone.html', {
                'error': 'مهلت این عملیات به پایان رسیده. لطفاً دوباره از ابتدا اقدام کنید.',
            })

        new_password = request.POST.get('new_password', '')
        confirm_password = request.POST.get('confirm_password', '')

        if new_password != confirm_password:
            return render(request, self.set_template, {'phone_number': phone_number, 'error': 'رمز عبور و تکرار آن یکسان نیستند.'})

        user = CustomUser.objects.filter(phone_number=phone_number).first()
        if not user:
            return render(request, self.set_template, {'phone_number': phone_number, 'error': 'حساب کاربری پیدا نشد.'})

        try:
            validate_password(new_password, user)
        except ValidationError as e:
            return render(request, self.set_template, {'phone_number': phone_number, 'error': ' '.join(e.messages)})

        user.set_password(new_password)
        user.save(update_fields=['password'])

        request.session.pop('reset_password_phone', None)
        request.session.pop('reset_password_verified_at', None)

        login(request, user)
        response = HttpResponse()
        response['HX-Redirect'] = '/'
        return response


class GoogleLoginRedirectView(View):
    """ ساخت لینک استاندارد OAuth2 گوگل و ریدایرکت کاربر به صفحه‌ی رضایت گوگل """
    AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'

    def get(self, request, *args, **kwargs):
        state = secrets.token_urlsafe(24)
        request.session['google_oauth_state'] = state

        params = {
            'client_id': settings.GOOGLE_CLIENT_ID,
            'redirect_uri': settings.GOOGLE_REDIRECT_URI,
            'response_type': 'code',
            'scope': 'openid email profile',
            'state': state,
            'prompt': 'select_account',
        }
        return redirect(f'{self.AUTH_URL}?{urlencode(params)}')


class GoogleLoginCallbackView(View):
    """
    بازگشت از گوگل: تبادل code با توکن، گرفتن ایمیل/شناسه‌ی کاربر گوگل، و اتصال به حساب موجود.
    طبق تصمیم پروژه: اگر حسابی با این ایمیل/شناسه پیدا نشود، حساب جدید ساخته نمی‌شود (ثبت‌نام فقط با
    موبایل انجام می‌شود)؛ کاربر به صفحه‌ی ورود با پیام راهنما هدایت می‌شود.
    """
    TOKEN_URL = 'https://oauth2.googleapis.com/token'
    USERINFO_URL = 'https://openidconnect.googleapis.com/v1/userinfo'

    def get(self, request, *args, **kwargs):
        error = request.GET.get('error')
        if error:
            return redirect(f"{reverse('accounts:login_view')}?google_error=denied")

        state = request.GET.get('state')
        expected_state = request.session.pop('google_oauth_state', None)
        if not state or not expected_state or state != expected_state:
            return redirect(f"{reverse('accounts:login_view')}?google_error=state")

        code = request.GET.get('code')
        if not code:
            return redirect(f"{reverse('accounts:login_view')}?google_error=missing_code")

        try:
            token_response = requests.post(self.TOKEN_URL, data={
                'code': code,
                'client_id': settings.GOOGLE_CLIENT_ID,
                'client_secret': settings.GOOGLE_CLIENT_SECRET,
                'redirect_uri': settings.GOOGLE_REDIRECT_URI,
                'grant_type': 'authorization_code',
            }, timeout=10)
            token_response.raise_for_status()
            access_token = token_response.json().get('access_token')

            userinfo_response = requests.get(
                self.USERINFO_URL,
                headers={'Authorization': f'Bearer {access_token}'},
                timeout=10,
            )
            userinfo_response.raise_for_status()
            userinfo = userinfo_response.json()
        except requests.RequestException:
            return redirect(f"{reverse('accounts:login_view')}?google_error=network")

        google_sub = userinfo.get('sub')
        google_email = (userinfo.get('email') or '').strip()

        user = CustomUser.objects.filter(google_sub=google_sub).first() if google_sub else None

        if user is None and google_email:
            user = CustomUser.objects.filter(email__iexact=google_email).first()
            if user and google_sub:
                user.google_sub = google_sub
                user.save(update_fields=['google_sub'])

        if user is None:
            return redirect(f"{reverse('accounts:login_view')}?google_error=not_found")

        login(request, user)
        return redirect('/')


class LogoutView(View):
    """ کلاس خروج از حساب کاربری """
    def post(self, request, *args, **kwargs):
        logout(request)
        # پس از خروج، کاربر را به صفحه اصلی ریدایرکت می‌کنیم
        response = HttpResponse()
        response['HX-Redirect'] = '/'
        return response
    
# ۱. کلاس ویرایش شده برای پروفایل و آدرس
class ProfileCompleteView(LoginRequiredMixin, View):
    template_name = 'accounts/profile_complete.html'
    partial_template = 'accounts/partials/profile_form.html'

    def get(self, request, *args, **kwargs):
        if request.user.status == UserStatus.ACTIVE:
            return redirect('accounts:dashboard') # ریدایرکت به داشبورد در صورت فعال بودن
            
        if request.headers.get('HX-Request'):
            return render(request, self.partial_template)
        return render(request, self.template_name)

    def post(self, request, *args, **kwargs):
        user = request.user
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        national_code = request.POST.get('national_code', '').strip()
        state = request.POST.get('state', '').strip()
        city = request.POST.get('city', '').strip()
        postal_code = request.POST.get('postal_code', '').strip()
        address = request.POST.get('address', '').strip()
        password = request.POST.get('password', '')
        confirm_password = request.POST.get('confirm_password', '')

        # کانتکست برای بازگرداندن مقادیر در صورت خطا
        context = {
            'first_name': first_name, 'last_name': last_name,
            'national_code': national_code, 'state': state,
            'city': city, 'postal_code': postal_code, 'address': address
        }

        # اعتبارسنجی فیلدها
        if not all([first_name, last_name, national_code, state, city, postal_code, address, password, confirm_password]):
            context['error'] = 'تکمیل تمامی فیلدها الزامی است.'
            return render(request, self.partial_template, context)

        if not national_code.isdigit() or len(national_code) != 10:
            context['error'] = 'کد ملی باید ۱۰ رقم عددی باشد.'
            return render(request, self.partial_template, context)

        if not postal_code.isdigit() or len(postal_code) != 10:
            context['error'] = 'کد پستی باید ۱۰ رقم عددی باشد.'
            return render(request, self.partial_template, context)

        if password != confirm_password:
            context['error'] = 'رمز عبور و تکرار آن یکسان نیستند.'
            return render(request, self.partial_template, context)

        try:
            validate_password(password, user)
        except ValidationError as e:
            context['error'] = ' '.join(e.messages)
            return render(request, self.partial_template, context)

        # ذخیره نهایی دیتای آدرس و پروفایل
        user.first_name = first_name
        user.last_name = last_name
        user.national_code = national_code
        user.state = state
        user.city = city
        user.postal_code = postal_code
        user.address = address
        user.set_password(password)
        user.status = UserStatus.PENDING_ERP_SYNC
        user.save()
        # چون رمز عوض شد، بدون این خط کاربر همین لحظه (با ریدایرکت زیر) از سشن خارج می‌شد
        update_session_auth_hash(request, user)

        # شلیک تسک به سلری پس‌زمینه
        sync_user_to_holoo.delay(user.id)
        # ارسال پیامک اطلاع‌رسانی به مدیر سایت
        from services.sms import send_sms
        from django.conf import settings
        admin_msg = f"مدیر گرامی، مشتری جدید ({first_name} {last_name} - {user.phone_number}) پروفایل خود را تکمیل کرد. لطفاً سطح قیمت ایشان را در هلو یا پنل بررسی نمایید."
        send_sms(settings.ADMIN_PHONE_NUMBER, admin_msg)

        response = HttpResponse()
        response['HX-Redirect'] = '/'
        return response


def _jalali_md(date_obj):
    """ برچسب روز/ماه شمسی (مثلاً 04/28) برای محور نمودار """
    j = jdatetime.date.fromgregorian(date=date_obj)
    return f"{j.month:02d}/{j.day:02d}"


def _jalali_ym(date_obj):
    """ برچسب سال/ماه شمسی (مثلاً 1404/04) برای محور نمودار """
    j = jdatetime.date.fromgregorian(date=date_obj)
    return f"{j.year}/{j.month:02d}"


def _build_order_activity_chart(user):
    """
    داده‌ی نمودار فعالیت خرید کاربر (تعداد سفارش) در سه بازه‌ی هفته/ماه/سال،
    کاملاً بر پایه‌ی سفارش‌های واقعی کاربر (Order.created_at)، بدون هیچ داده‌ی ساختگی.
    برچسب‌های محور نمودار شمسی هستند؛ گروه‌بندی داخلی بر پایه‌ی تاریخ میلادی ذخیره‌شده باقی می‌ماند.
    """
    now = timezone.localtime()
    orders = list(Order.objects.filter(user=user).values_list('created_at', flat=True))

    # --- هفته: ۷ روز گذشته، به تفکیک روز ---
    week_labels, week_data = [], []
    for i in range(6, -1, -1):
        day = (now - timedelta(days=i)).date()
        week_labels.append(_jalali_md(day))
        week_data.append(sum(1 for dt in orders if timezone.localtime(dt).date() == day))

    # --- ماه: ۳۰ روز گذشته، به تفکیک هفته (۵ بازه) ---
    month_labels, month_data = [], []
    for i in range(4, -1, -1):
        start = (now - timedelta(days=(i + 1) * 6 + i)).date()
        end = (now - timedelta(days=i * 7)).date()
        month_labels.append(f"{_jalali_md(start)} تا {_jalali_md(end)}")
        month_data.append(sum(1 for dt in orders if start <= timezone.localtime(dt).date() <= end))

    # --- سال: ۱۲ ماه گذشته، به تفکیک ماه میلادی (چون تاریخ ذخیره‌شده میلادی است) ---
    year_labels, year_data = [], []
    for i in range(11, -1, -1):
        ref = now - timedelta(days=i * 30)
        year_labels.append(_jalali_ym(ref.date()))
        year_data.append(sum(1 for dt in orders if timezone.localtime(dt).strftime('%Y/%m') == ref.strftime('%Y/%m')))

    return {
        'week': {'labels': week_labels, 'data': week_data},
        'month': {'labels': month_labels, 'data': month_data},
        'year': {'labels': year_labels, 'data': year_data},
    }


class DashboardView(LoginRequiredMixin, TemplateView):
    """ پیشخوان اصلی پنل کاربری: آمار واقعی حساب، نمودار سفارش‌ها، آخرین سفارش‌ها و تراکنش‌ها """
    template_name = 'accounts/dashboard.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        user_orders = Order.objects.filter(user=user)

        context['active_nav'] = 'dashboard'
        context['total_orders_count'] = user_orders.count()
        context['pending_orders_count'] = user_orders.filter(status__in=['pending', 'registered']).count()
        context['favorites_count'] = FavoriteProduct.objects.filter(user=user).count()
        context['recently_viewed_count'] = RecentlyViewed.objects.filter(user=user).count()
        context['recent_orders'] = user_orders.order_by('-created_at')[:5]
        context['recent_transactions'] = Transaction.objects.filter(user=user, status='success').order_by('-updated_at')[:5]
        context['loyalty_points'] = user.get_loyalty_points()
        current_level, next_level, remaining = user.get_loyalty_level()
        context['loyalty_level'] = current_level
        context['loyalty_next_level'] = next_level
        context['loyalty_remaining'] = remaining
        context['loyalty_progress_percent'] = user.get_loyalty_progress_percent()
        context['chart_data_json'] = json.dumps(_build_order_activity_chart(user))
        return context


class ProfileView(LoginRequiredMixin, View):
    """ نمایش و ویرایش پروفایل کاربر (صفحه‌ی کامل، مطابق پروفایل کاربری قالب) """
    template_name = 'accounts/profile.html'

    def get(self, request, *args, **kwargs):
        return render(request, self.template_name, {'active_nav': 'profile'})

    def post(self, request, *args, **kwargs):
        user = request.user
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        national_code = request.POST.get('national_code', '').strip()
        email = request.POST.get('email', '').strip()
        birth_date = request.POST.get('birth_date', '').strip()
        state = request.POST.get('state', '').strip()
        city = request.POST.get('city', '').strip()
        postal_code = request.POST.get('postal_code', '').strip()
        address = request.POST.get('address', '').strip()

        context = {
            'active_nav': 'profile',
            'first_name': first_name, 'last_name': last_name, 'national_code': national_code,
            'email': email, 'birth_date': birth_date,
            'state': state, 'city': city, 'postal_code': postal_code, 'address': address,
        }

        if not all([first_name, last_name, national_code, state, city, postal_code, address]):
            context['error'] = 'تکمیل تمامی فیلدهای الزامی ضروری است.'
            return render(request, self.template_name, context)

        if not national_code.isdigit() or len(national_code) != 10:
            context['error'] = 'کد ملی باید ۱۰ رقم عددی باشد.'
            return render(request, self.template_name, context)

        if not postal_code.isdigit() or len(postal_code) != 10:
            context['error'] = 'کد پستی باید ۱۰ رقم عددی باشد.'
            return render(request, self.template_name, context)

        user.first_name = first_name
        user.last_name = last_name
        user.national_code = national_code
        user.email = email or None
        if birth_date:
            try:
                user.birth_date = datetime.strptime(birth_date, '%Y-%m-%d').date()
            except ValueError:
                context['error'] = 'قالب تاریخ تولد نامعتبر است.'
                return render(request, self.template_name, context)
        user.state = state
        user.city = city
        user.postal_code = postal_code
        user.address = address
        user.status = UserStatus.PENDING_ERP_SYNC
        user.save()

        sync_user_to_holoo.delay(user.id)

        context['success'] = True
        context.pop('error', None)
        return render(request, self.template_name, context)


class ChangePasswordView(LoginRequiredMixin, View):
    """ صفحه‌ی تغییر رمز عبور در پنل کاربری (بخش برگرفته از قالب خریداری‌شده) """
    template_name = 'accounts/change_password.html'

    def get(self, request, *args, **kwargs):
        return render(request, self.template_name, {
            'active_nav': 'change_password',
            'has_password': request.user.has_real_password(),
        })

    def post(self, request, *args, **kwargs):
        user = request.user
        has_password = user.has_real_password()
        current_password = request.POST.get('current_password', '')
        new_password = request.POST.get('new_password', '')
        confirm_password = request.POST.get('confirm_password', '')

        context = {'active_nav': 'change_password', 'has_password': has_password}

        if has_password and not user.check_password(current_password):
            context['error'] = 'رمز عبور فعلی اشتباه است.'
            return render(request, self.template_name, context)

        if new_password != confirm_password:
            context['error'] = 'رمز عبور جدید و تکرار آن یکسان نیستند.'
            return render(request, self.template_name, context)

        try:
            validate_password(new_password, user)
        except ValidationError as e:
            context['error'] = ' '.join(e.messages)
            return render(request, self.template_name, context)

        user.set_password(new_password)
        user.save(update_fields=['password'])
        # تا کاربر بعد از تغییر رمز از سشن خارج نشود
        update_session_auth_hash(request, user)

        context['success'] = True
        context['has_password'] = True
        return render(request, self.template_name, context)


class ProfileAvatarUploadView(LoginRequiredMixin, View):
    """ آپلود/تغییر تصویر پروفایل """

    def post(self, request, *args, **kwargs):
        avatar = request.FILES.get('avatar')
        if avatar:
            request.user.avatar = avatar
            request.user.save(update_fields=['avatar'])
        return redirect('accounts:profile')


class WalletView(LoginRequiredMixin, TemplateView):
    """ نمایش موجودی کیف پول کاربر (فقط نمایشی؛ شارژ/انتقال هنوز راه‌اندازی نشده) """
    template_name = 'accounts/wallet.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['active_nav'] = 'wallet'
        return context


class ComingSoonView(LoginRequiredMixin, TemplateView):
    """
    ویوی عمومی برای بخش‌هایی از قالب که هنوز بک‌اند واقعی ندارند
    (تیکت، دیدگاه، تخفیف، اعلان). هر بخش با as_view(section_title=..., active_nav=...) ثبت می‌شود.
    """
    template_name = 'accounts/coming_soon.html'
    section_title = 'این بخش'
    active_nav = None

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['section_title'] = self.section_title
        context['active_nav'] = self.active_nav
        return context

