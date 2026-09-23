import json
import secrets
import jdatetime
import requests
from datetime import timedelta, datetime
from urllib.parse import urlencode
from django.db import transaction
from django.utils import timezone
from django.utils.encoding import iri_to_uri
from django.utils.http import url_has_allowed_host_and_scheme
from django.shortcuts import render, redirect
from django.urls import reverse
from django.conf import settings
from django.contrib.auth import login, logout, authenticate, update_session_auth_hash
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.views import View  # ایمپورت کلاس پایه ویوها
from django.views.generic import TemplateView
from django.http import HttpResponse
from django.template.loader import render_to_string
from .models import ApprovalStatus, CustomUser, OTPRequest, OTPPurpose, normalize_phone_number, UserStatus
from .captcha import new_captcha, get_captcha_code, render_captcha_png, verify_captcha
from .forms import ChangePasswordForm, ProfileCompleteForm, ProfileEditForm
from .throttle import ThrottleError, check_otp_quota, consume_otp_quota, get_client_ip, reset_otp_quota
from notifications.service import notify
from django.contrib.auth.mixins import LoginRequiredMixin # برای اجباری کردن لاگین
from .signals import profile_completed, profile_updated, user_registered
from .stats import collect as collect_stats


def _safe_next(request, raw_next):
    """
    مقدار next (صفحه‌ای که کاربر قبل از لاگین آنجا بود) را اعتبارسنجی می‌کند تا کسی نتواند با
    ساختن لینکی مثل ?next=https://evil.com کاربر را بعد از ورود به یک سایت جعلی بفرستد (Open Redirect).
    iri_to_uri لازم است چون هدر HX-Redirect (برخلاف Location در ریدایرکت معمولی جنگو) خودکار
    درست انکود نمی‌شود؛ بدون آن، مسیرهای فارسی (اسلاگ محصول و ...) باعث MIME-encode شدن کل هدر
    می‌شوند و htmx دیگر آن را به‌عنوان یک URL معتبر تشخیص نمی‌دهد.
    """
    if raw_next and url_has_allowed_host_and_scheme(
        url=raw_next, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return iri_to_uri(raw_next)
    return '/'


def _captcha_feedback(request, error=None, captcha_key=None):
    """ پاسخ مشترک «متن خطا + (در صورت لزوم) ویجت کپچای تازه» برای فرم‌های رمز عبور/OTP """
    html = render_to_string('accounts/partials/captcha_feedback.html', {
        'error': error, 'captcha_key': captcha_key,
    }, request=request)
    return HttpResponse(html)


class CaptchaImageView(View):
    """ تصویر PNG کپچای عددی متناظر با یک کلید سشن """

    def get(self, request, key, *args, **kwargs):
        code = get_captcha_code(request, key)
        if not code:
            return HttpResponse(status=404)
        return HttpResponse(render_captcha_png(code), content_type='image/png')


class CaptchaRefreshView(View):
    """ دکمه‌ی «تغییر کد»: یک کپچای تازه می‌سازد و فقط خودِ ویجت را برمی‌گرداند """

    def get(self, request, *args, **kwargs):
        key = new_captcha(request)
        return render(request, 'accounts/partials/captcha_widget.html', {'captcha_key': key})


class LoginView(View):
    """ کلاس مدیریت صفحه اصلی لاگین """
    template_name = 'accounts/login.html'

    def get(self, request, *args, **kwargs):
        next_url = request.GET.get('next', '')
        # اگر کاربر قبلاً لاگین کرده بود، به صفحه اصلی (یا next) برود
        if request.user.is_authenticated:
            return redirect(_safe_next(request, next_url))

        return render(request, self.template_name, {'next': next_url})


class PhoneFormView(View):
    """ بازگرداندن مرحله‌ی اول (شماره موبایل) بدون رفرش کل صفحه/مودال """
    template_name = 'accounts/partials/phone_step.html'

    def get(self, request, *args, **kwargs):
        return render(request, self.template_name, {'next': request.GET.get('next', '')})


class LoginTabsView(View):
    """ بازگرداندن کل تب‌بندی ورود (رمز عبور / پیامک) - برای بازگشت از مسیر فراموشی رمز """
    template_name = 'accounts/partials/login_tabs.html'

    def get(self, request, *args, **kwargs):
        return render(request, self.template_name, {'next': request.GET.get('next', '')})


class SendOTPView(View):
    """ کلاس دریافت شماره موبایل با HTMX، تولید و ارسال کد """
    phone_template = 'accounts/partials/phone_step.html'
    otp_template = 'accounts/partials/otp_form.html'

    def post(self, request, *args, **kwargs):
        raw_phone = request.POST.get('phone_number')
        next_url = request.POST.get('next', '')

        try:
            # 1. نرمال‌سازی شماره موبایل
            phone_number = normalize_phone_number(raw_phone)
        except ValueError as e:
            return render(request, self.phone_template, {'error': str(e), 'next': next_url})

        # 2. محدودیت نرخ: بدون این، می‌شد بی‌نهایت پیامک برای یک شماره فرستاد
        client_ip = get_client_ip(request)
        try:
            check_otp_quota(phone_number, client_ip)
        except ThrottleError as e:
            return render(request, self.phone_template, {'error': str(e), 'next': next_url})

        # 3. تولید کد ۶ رقمی و ذخیره با انقضای ۲ دقیقه‌ای
        code = str(secrets.randbelow(900000) + 100000)
        OTPRequest.objects.create(
            phone_number=phone_number,
            code=code,
            purpose=OTPPurpose.REGISTER_LOGIN,
            ip_address=client_ip,
            expires_at=timezone.now() + timedelta(minutes=2),
        )

        # 4. ارسال کد تایید (کانال ارسال را اپ notifications تعیین می‌کند)
        notify(phone_number, 'otp', code=code)
        consume_otp_quota(phone_number, client_ip)

        # 5. برگرداندن فرم دوم
        return render(request, self.otp_template, {'phone_number': phone_number, 'next': next_url})


class VerifyOTPView(View):
    """ کلاس دریافت کد تایید با HTMX، بررسی صحت آن و لاگین کاربر """
    otp_template = 'accounts/partials/otp_form.html'

    def post(self, request, *args, **kwargs):
        phone_number = request.POST.get('phone_number')
        code = request.POST.get('code')

        # بعد از ۳ تلاش غلط روی همین کد، قبل از حتی بررسی خودِ کد، کپچا را می‌خواهیم
        if OTPRequest.captcha_required(phone_number):
            if not verify_captcha(request, request.POST.get('captcha_key'), request.POST.get('captcha_answer')):
                return _captcha_feedback(request, error='کد امنیتی وارد شده صحیح نیست.', captcha_key=new_captcha(request))

        # فراخوانی متد هوشمند مدل برای بررسی صحت و انقضا
        is_valid, error_message, attempt_count = OTPRequest.verify_code(phone_number, code)

        if not is_valid:
            # فقط پیام خطا (و در صورت نیاز کپچا) به صورت HTML برگردانده می‌شود تا داخل کانتینر خطا لود شود 👇
            # این کار باعث می‌شود فرم و اسکریپت تایمر اصلاً دست نخورند و ریست نشوند
            show_captcha = attempt_count >= OTPRequest.CAPTCHA_THRESHOLD
            return _captcha_feedback(request, error=error_message, captcha_key=new_captcha(request) if show_captcha else None)

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
            # فقط برای شماره‌ی *واقعاً تازه* (created=True)؛ ورود دوباره‌ی کاربر از‌قبل‌موجود
            # با همین OTP هرگز این سیگنال را نمی‌گیرد (اطلاع فوری ثبت‌نام جدید به مدیر)
            user_registered.send_robust(sender=CustomUser, user=user)

        # ورود موفق یعنی صاحب واقعی شماره است؛ سقف ارسال آزاد می‌شود تا کاربر درست
        # به‌خاطر تلاش‌های قبلی‌اش تا یک ساعت قفل نماند
        reset_otp_quota(phone_number)
        login(request, user)

        # ریدایرکت کل صفحه با هدر HTMX به همان صفحه‌ای که کاربر قبل از لاگین آنجا بود
        response = HttpResponse()
        response['HX-Redirect'] = _safe_next(request, request.POST.get('next', ''))
        return response

class LoginWithPasswordView(View):
    """
    ورود با شماره موبایل + رمز عبور (تب دوم پاپ‌آپ ورود).
    برخلاف مراحل OTP (که کل .auth-step-container را جایگزین می‌کنند)، این ویو فقط یک قطعه‌ی
    خطا+کپچا برمی‌گرداند (هدف hx-post روی #password-feedback است) تا با ورود اشتباه، نوار تب‌ها
    از بین نرود و کاربر همان‌جا دوباره تلاش کند. کپچا همیشه لازم است (نه فقط بعد از چند تلاش).
    """

    def post(self, request, *args, **kwargs):
        raw_phone = request.POST.get('phone_number', '')
        password = request.POST.get('password', '')

        try:
            phone_number = normalize_phone_number(raw_phone)
        except ValueError as e:
            return _captcha_feedback(request, error=str(e), captcha_key=new_captcha(request))

        if not verify_captcha(request, request.POST.get('captcha_key'), request.POST.get('captcha_answer')):
            return _captcha_feedback(request, error='کد امنیتی وارد شده صحیح نیست.', captcha_key=new_captcha(request))

        user = CustomUser.objects.filter(phone_number=phone_number).first()
        if user and not user.has_real_password():
            return _captcha_feedback(
                request,
                error='برای این شماره هنوز رمز عبوری تعیین نشده. '
                      'از تب «ورود با پیامک» استفاده کنید یا از بخش «رمز عبور را فراموش کرده‌اید» یک رمز تعیین کنید.',
                captcha_key=new_captcha(request),
            )

        authenticated_user = authenticate(request, username=phone_number, password=password)
        if authenticated_user is None:
            return _captcha_feedback(request, error='شماره موبایل یا رمز عبور اشتباه است.', captcha_key=new_captcha(request))

        login(request, authenticated_user)
        response = HttpResponse()
        response['HX-Redirect'] = _safe_next(request, request.POST.get('next', ''))
        return response


class ForgotPasswordSendOTPView(View):
    """ گام اول بازیابی رمز عبور: گرفتن شماره موبایل و ارسال کد یکبارمصرف """
    phone_template = 'accounts/partials/forgot_password_phone.html'
    otp_template = 'accounts/partials/forgot_password_otp.html'

    def get(self, request, *args, **kwargs):
        """ نمایش فرم اولیه‌ی «فراموشی رمز» (لینک از تب ورود با رمز عبور) """
        return render(request, self.phone_template, {'next': request.GET.get('next', '')})

    def post(self, request, *args, **kwargs):
        raw_phone = request.POST.get('phone_number')
        next_url = request.POST.get('next', '')

        try:
            phone_number = normalize_phone_number(raw_phone)
        except ValueError as e:
            return render(request, self.phone_template, {'error': str(e), 'next': next_url})

        if not CustomUser.objects.filter(phone_number=phone_number).exists():
            return render(request, self.phone_template, {'error': 'حسابی با این شماره موبایل پیدا نشد.', 'next': next_url})

        # همان سقف مسیر ورود؛ عمداً شمارنده‌ی مشترک است تا نشود با جابه‌جایی بین دو فرم
        # (ورود / فراموشی رمز) محدودیت را دو برابر کرد
        client_ip = get_client_ip(request)
        try:
            check_otp_quota(phone_number, client_ip)
        except ThrottleError as e:
            return render(request, self.phone_template, {'error': str(e), 'next': next_url})

        code = str(secrets.randbelow(900000) + 100000)
        OTPRequest.objects.create(
            phone_number=phone_number,
            code=code,
            purpose=OTPPurpose.RESET_PASSWORD,
            ip_address=client_ip,
            expires_at=timezone.now() + timedelta(minutes=2),
        )
        notify(phone_number, 'otp', code=code)
        consume_otp_quota(phone_number, client_ip)

        return render(request, self.otp_template, {'phone_number': phone_number, 'next': next_url})


class ForgotPasswordVerifyView(View):
    """ گام دوم بازیابی رمز عبور: بررسی کد و در صورت صحت، نمایش فرم تعیین رمز جدید """
    otp_template = 'accounts/partials/forgot_password_otp.html'
    set_template = 'accounts/partials/forgot_password_set.html'

    def post(self, request, *args, **kwargs):
        phone_number = request.POST.get('phone_number')
        code = request.POST.get('code')
        next_url = request.POST.get('next', '')

        if OTPRequest.captcha_required(phone_number, purpose=OTPPurpose.RESET_PASSWORD):
            if not verify_captcha(request, request.POST.get('captcha_key'), request.POST.get('captcha_answer')):
                return _captcha_feedback(request, error='کد امنیتی وارد شده صحیح نیست.', captcha_key=new_captcha(request))

        is_valid, error_message, attempt_count = OTPRequest.verify_code(phone_number, code, purpose=OTPPurpose.RESET_PASSWORD)
        if not is_valid:
            show_captcha = attempt_count >= OTPRequest.CAPTCHA_THRESHOLD
            return _captcha_feedback(request, error=error_message, captcha_key=new_captcha(request) if show_captcha else None)

        # تایید هویت با موبایل کامل نشد؛ فقط شماره‌ی تاییدشده در سشن نگه داشته می‌شود تا گام بعد
        # (تعیین رمز جدید) از سمت کاربر قابل دستکاری نباشد (به‌جای اعتماد به فیلد مخفی فرم)
        request.session['reset_password_phone'] = phone_number
        request.session['reset_password_verified_at'] = timezone.now().isoformat()
        request.session['reset_password_next'] = next_url

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
            request.session.pop('reset_password_next', None)
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

        next_url = request.session.get('reset_password_next', '')
        request.session.pop('reset_password_phone', None)
        request.session.pop('reset_password_verified_at', None)
        request.session.pop('reset_password_next', None)

        login(request, user)
        response = HttpResponse()
        response['HX-Redirect'] = _safe_next(request, next_url)
        return response


class GoogleLoginRedirectView(View):
    """ ساخت لینک استاندارد OAuth2 گوگل و ریدایرکت کاربر به صفحه‌ی رضایت گوگل """
    AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'

    def get(self, request, *args, **kwargs):
        state = secrets.token_urlsafe(24)
        request.session['google_oauth_state'] = state
        request.session['google_login_next'] = request.GET.get('next', '')

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
        next_url = request.session.pop('google_login_next', '')
        return redirect(_safe_next(request, next_url))


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
        form = ProfileCompleteForm(request.POST, instance=request.user)
        if not form.is_valid():
            # قالب فعلی مقادیر را تک‌تک از کانتکست می‌خواند و یک {{ error }} نمایش می‌دهد
            return render(request, self.partial_template, {
                **{name: request.POST.get(name, '') for name in form.Meta.fields},
                'error': form.error_text,
            })

        # قبل از save، وگرنه بعدش همیشه PENDING_ERP_SYNC است و «اولین بار» دیگر قابل تشخیص نیست.
        # بدون این گارد، رفرش صفحه یا ساب‌میت دوباره‌ی همین فرم (هر دو مجازند چون ProfileCompleteView.get
        # فقط روی status=ACTIVE ریدایرکت می‌کند، نه هر باری که پروفایل کامل شد) پیامک تکراری به مدیر می‌زد.
        was_pending_profile = request.user.status == UserStatus.PENDING_PROFILE

        user = form.save(commit=False)
        user.set_password(form.cleaned_data['password'])
        user.status = UserStatus.PENDING_ERP_SYNC
        user.save()
        # چون رمز عوض شد، بدون این خط کاربر همین لحظه (با ریدایرکت زیر) از سشن خارج می‌شد
        update_session_auth_hash(request, user)

        # اعلام رویداد؛ همگام‌سازی با حسابداری و اطلاع‌رسانی به مدیر را شنونده‌ها انجام می‌دهند —
        # فقط «اولین بار» (نگاه کنید was_pending_profile بالا)
        if was_pending_profile:
            profile_completed.send_robust(sender=CustomUser, user=user)

        response = HttpResponse()
        response['HX-Redirect'] = '/'
        return response


class DashboardView(LoginRequiredMixin, TemplateView):
    """
    پیشخوان اصلی پنل کاربری.

    داده‌ی هر بخش از رجیستری accounts.stats خوانده می‌شود؛ خودِ محاسبه در اپ صاحب آن داده
    انجام می‌شود (orders/stats.py, payments/stats.py, ...). به این ترتیب این ویو دیگر مدل
    هیچ اپ دیگری را import نمی‌کند و اگر اپی از پروژه حذف شود، پیشخوان نمی‌شکند.
    """
    template_name = 'accounts/dashboard.html'

    STAT_DEFAULTS = {
        'orders_total': 0,
        'orders_pending': 0,
        'orders_recent': [],
        'orders_activity_chart': {},
        'favorites_count': 0,
        'recently_viewed_count': 0,
        'transactions_recent': [],
    }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        stats = collect_stats(user, self.STAT_DEFAULTS)

        context['active_nav'] = 'dashboard'
        context['total_orders_count'] = stats['orders_total']
        context['pending_orders_count'] = stats['orders_pending']
        context['favorites_count'] = stats['favorites_count']
        context['recently_viewed_count'] = stats['recently_viewed_count']
        context['recent_orders'] = stats['orders_recent']
        context['recent_transactions'] = stats['transactions_recent']
        context['chart_data_json'] = json.dumps(stats['orders_activity_chart'])

        context['loyalty_points'] = user.get_loyalty_points()
        current_level, next_level, remaining = user.get_loyalty_level()
        context['loyalty_level'] = current_level
        context['loyalty_next_level'] = next_level
        context['loyalty_remaining'] = remaining
        context['loyalty_progress_percent'] = user.get_loyalty_progress_percent()
        return context


class ProfileView(LoginRequiredMixin, View):
    """ نمایش و ویرایش پروفایل کاربر (صفحه‌ی کامل، مطابق پروفایل کاربری قالب) """
    template_name = 'accounts/profile.html'

    def get(self, request, *args, **kwargs):
        context = {'active_nav': 'profile'}
        if request.user.birth_date:
            context['birth_date_jalali'] = jdatetime.date.fromgregorian(date=request.user.birth_date).strftime('%Y/%m/%d')
        return render(request, self.template_name, context)

    def post(self, request, *args, **kwargs):
        form = ProfileEditForm(request.POST, instance=request.user)

        # قالب فعلی مقادیر را تک‌تک از کانتکست می‌خواند (نه از آبجکت فرم)، پس همان‌ها را
        # عیناً برمی‌گردانیم تا ورودی کاربر با خطا پاک نشود
        context = {
            'active_nav': 'profile',
            'birth_date_jalali': request.POST.get('birth_date', ''),
            **{name: request.POST.get(name, '') for name in ('first_name', 'last_name', 'national_code',
                                                             'email')},
        }

        if not form.is_valid():
            context['error'] = form.error_text
            return render(request, self.template_name, context)

        # فقط این سه فیلد «هویتی»اند (نگاه کنید CustomUser.revoke_approval_due_to_identity_change)؛
        # ایمیل/تاریخ‌تولد/آواتار و... جزو form.changed_data می‌آیند ولی هرگز تأیید را باطل نمی‌کنند
        identity_changed = bool(set(form.changed_data) & {'first_name', 'last_name', 'national_code'})
        was_approved = request.user.approval_status == ApprovalStatus.APPROVED

        # ذخیره‌ی فیلدهای تازه و ابطال احتمالی تأیید در یک تراکنش مشترک: revoke_approval_due_to_identity_change
        # خودش select_for_update می‌زند، ولی چون همان ردیف از قبل توسط این تراکنش قفل شده (form.save پایین‌تر
        # آن را می‌نویسد)، این فقط تکرار همان قفل است نه قفل تازه/بن‌بست — نتیجه یک واحد اتمیک واقعی می‌شود
        # که با approve()ی هم‌زمان مدیر روی همان ردیف سریال می‌شود (نگاه کنید توضیح خودِ آن متد در models.py)
        with transaction.atomic():
            user = form.save(commit=False)
            user.status = UserStatus.PENDING_ERP_SYNC
            user.save()
            if identity_changed and was_approved:
                user.revoke_approval_due_to_identity_change()

        profile_updated.send_robust(sender=CustomUser, user=user)

        context['success'] = True
        return render(request, self.template_name, context)


class ResubmitForReviewView(LoginRequiredMixin, View):
    """
    دکمه‌ی «ارسال مجدد جهت بررسی» در پنل کاربری؛ فقط برای کاربرِ REJECTED معنا دارد. صرفِ ویرایش
    پروفایل هیچ‌وقت approval_status را برنمی‌گرداند (نگاه کنید ProfileView بالا) — تنها همین اکشنِ
    صریح این کار را می‌کند. idempotency/قفل هم‌زمانی همگی داخل خودِ resubmit_for_review() است؛
    این ویو فقط changed را می‌خواند تا بداند اعلان لازم است یا نه (خودِ اعلان از طریق
    notifications.receivers، روی سیگنال user_resubmitted_for_review صادر می‌شود).
    """

    def post(self, request, *args, **kwargs):
        request.user.resubmit_for_review()
        return redirect('accounts:dashboard')


class ChangePasswordView(LoginRequiredMixin, View):
    """ صفحه‌ی تغییر رمز عبور در پنل کاربری (بخش برگرفته از قالب خریداری‌شده) """
    template_name = 'accounts/change_password.html'

    def get(self, request, *args, **kwargs):
        return render(request, self.template_name, {
            'active_nav': 'change_password',
            'has_password': request.user.has_real_password(),
        })

    def post(self, request, *args, **kwargs):
        form = ChangePasswordForm(request.user, request.POST)
        context = {'active_nav': 'change_password', 'has_password': form.has_password}

        if not form.is_valid():
            context['error'] = form.error_text
            return render(request, self.template_name, context)

        form.save()
        # تا کاربر بعد از تغییر رمز از سشن خارج نشود
        update_session_auth_hash(request, request.user)

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

