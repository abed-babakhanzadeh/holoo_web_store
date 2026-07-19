import random
import json
import jdatetime
from datetime import timedelta, datetime
from django.utils import timezone
from django.shortcuts import render, redirect
from django.contrib.auth import login, logout
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

        # کانتکست برای بازگرداندن مقادیر در صورت خطا
        context = {
            'first_name': first_name, 'last_name': last_name,
            'national_code': national_code, 'state': state,
            'city': city, 'postal_code': postal_code, 'address': address
        }

        # اعتبارسنجی فیلدها
        if not all([first_name, last_name, national_code, state, city, postal_code, address]):
            context['error'] = 'تکمیل تمامی فیلدها الزامی است.'
            return render(request, self.partial_template, context)

        if not national_code.isdigit() or len(national_code) != 10:
            context['error'] = 'کد ملی باید ۱۰ رقم عددی باشد.'
            return render(request, self.partial_template, context)

        if not postal_code.isdigit() or len(postal_code) != 10:
            context['error'] = 'کد پستی باید ۱۰ رقم عددی باشد.'
            return render(request, self.partial_template, context)

        # ذخیره نهایی دیتای آدرس و پروفایل
        user.first_name = first_name
        user.last_name = last_name
        user.national_code = national_code
        user.state = state
        user.city = city
        user.postal_code = postal_code
        user.address = address
        user.status = UserStatus.PENDING_ERP_SYNC
        user.save()

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

