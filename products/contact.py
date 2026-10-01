"""
صفحه‌های «درباره ما» و «تماس با ما».

محتوای هر دو صفحه کاملاً از SiteSettings (مرجع واحد مشخصات فروشگاه) خوانده می‌شود؛ این‌جا فقط فرم تماس،
ثبت پیام و محدودیت نرخ است. اعلان پیامکی به مدیر در همین فایل صدا زده نمی‌شود: ویو فقط سیگنال دامنه‌ی
products.signals.contact_message_received را اعلام می‌کند و notifications/receivers.py به آن گوش می‌دهد
(products به notifications وابسته نیست).
"""

import logging

from django import forms
from django.contrib import messages
from django.core.cache import cache
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View

from accounts.models import ApprovalStatus, normalize_phone_number
from accounts.throttle import get_client_ip
from .models import ContactMessage
from .signals import contact_message_received

logger = logging.getLogger(__name__)

# سقف پیام‌های یک IP در هر ساعت؛ شمارنده روی کش مشترک (Redis)، پس بین همه‌ی workerها معتبر است
MAX_MESSAGES_PER_IP_PER_HOUR = 5
THROTTLE_WINDOW = 3600
THROTTLE_KEY = 'contact:ip:{ip}'

SUCCESS_MESSAGE = 'پیام شما با موفقیت ثبت شد. همکاران ما در اولین فرصت با شما تماس خواهند گرفت.'
THROTTLED_MESSAGE = 'تعداد پیام‌های ارسالی از این دستگاه بیش از حد مجاز است. لطفاً بعداً دوباره تلاش کنید.'


def is_verified_customer(user):
    """
    مشتری واردشده‌ی تأییدشده (approval_status=APPROVED): هویتش (نام، موبایل، ایمیل) در پروفایل ثبت و تأیید شده و
    تغییر هویت تأیید را باطل می‌کند (CustomUser.revoke_approval_due_to_identity_change). برای همین فرم تماس
    از او چیزی نمی‌پرسد و مشخصات را سمت سرور از پروفایل می‌خواند، نه از مقدار ارسالی فرم (جعل‌ناپذیر).
    """
    return bool(user.is_authenticated and user.approval_status == ApprovalStatus.APPROVED)


def profile_full_name(user):
    return ' '.join(part for part in ((user.first_name or '').strip(), (user.last_name or '').strip()) if part)


class ContactForm(forms.Form):
    name = forms.CharField(max_length=100, label='نام کامل', error_messages={'required': 'نام خود را وارد کنید.'})
    phone = forms.CharField(max_length=20, required=False, label='شماره موبایل')
    email = forms.EmailField(required=False, label='ایمیل', error_messages={'invalid': 'ایمیل واردشده معتبر نیست.'})
    subject = forms.CharField(max_length=150, label='موضوع', error_messages={'required': 'موضوع پیام را وارد کنید.'})
    message = forms.CharField(
        max_length=2000, label='پیام شما', widget=forms.Textarea,
        error_messages={'required': 'متن پیام را وارد کنید.'},
    )
    # honeypot: انسان این فیلد (مخفی با CSS) را نمی‌بیند و خالی می‌گذارد؛ ربات‌ها معمولاً پرش می‌کنند
    website = forms.CharField(required=False)

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.verified = bool(user is not None and is_verified_customer(user))
        if self.verified:
            # مشخصات فرستنده از پروفایل می‌آید؛ این سه فیلد اصلاً در فرم نیستند، پس مقدار ارسالی‌شان هم نادیده می‌ماند
            for field in ('name', 'phone', 'email'):
                del self.fields[field]
        elif user is not None and user.is_authenticated and not self.is_bound:
            # مشتری واردشده ولی هنوز تأییدنشده: پروفایلش ممکن است ناقص باشد؛ فیلدها خالی نمی‌مانند ولی قابل ویرایش‌اند
            self.initial.setdefault('name', profile_full_name(user))
            self.initial.setdefault('phone', user.phone_number)
            self.initial.setdefault('email', user.email or '')

    def clean_name(self):
        return ' '.join(self.cleaned_data['name'].split())

    def clean_subject(self):
        return ' '.join(self.cleaned_data['subject'].split())

    def clean_phone(self):
        raw = self.cleaned_data.get('phone', '').strip()
        if not raw:
            return ''
        try:
            return normalize_phone_number(raw)
        except ValueError:
            raise forms.ValidationError('شماره موبایل واردشده معتبر نیست.')

    def clean_message(self):
        message = self.cleaned_data['message'].strip()
        if not message:
            raise forms.ValidationError('متن پیام را وارد کنید.')
        return message

    def clean(self):
        cleaned = super().clean()
        if self.verified:
            return cleaned
        # پاسخ‌گویی نیاز به یک راه ارتباطی دارد
        if not cleaned.get('phone') and not cleaned.get('email') and not self.errors.get('phone') and not self.errors.get('email'):
            raise forms.ValidationError('برای پاسخ‌گویی، حداقل یکی از «شماره موبایل» یا «ایمیل» را وارد کنید.')
        return cleaned


def _throttle_exceeded(ip):
    """ شمارش پنجره‌ای با cache.add (TTL با اولین پیام تثبیت می‌شود و با پیام‌های بعدی تمدید نمی‌شود) """
    key = THROTTLE_KEY.format(ip=ip)
    if cache.add(key, 1, THROTTLE_WINDOW):
        return False
    try:
        return cache.incr(key) > MAX_MESSAGES_PER_IP_PER_HOUR
    except ValueError:
        cache.set(key, 1, THROTTLE_WINDOW)
        return False


class AboutUsView(View):
    """ صفحه‌ی «درباره ما»؛ همه‌ی محتوا از context processor (site_settings) می‌آید """

    def get(self, request, *args, **kwargs):
        return render(request, 'products/about_us.html')


class ContactUsView(View):
    """ صفحه‌ی «تماس با ما» + ثبت پیام (الگوی POST/Redirect/GET با messages.success) """

    def _context(self, request, form):
        context = {'form': form, 'verified_identity': form.verified}
        if form.verified:
            context['sender_name'] = profile_full_name(request.user) or request.user.phone_number
            context['sender_phone'] = request.user.phone_number
        return context

    def get(self, request, *args, **kwargs):
        form = ContactForm(user=request.user)
        return render(request, 'products/contact_us.html', self._context(request, form))

    def post(self, request, *args, **kwargs):
        form = ContactForm(request.POST, user=request.user)
        if not form.is_valid():
            return render(request, 'products/contact_us.html', self._context(request, form), status=400)

        if form.cleaned_data.get('website'):
            # ربات: بی‌سروصدا موفق نشان بده، چیزی ثبت یا ارسال نکن
            messages.success(request, SUCCESS_MESSAGE)
            return redirect(reverse('products:contact_us'))

        ip = get_client_ip(request)
        if _throttle_exceeded(ip):
            form.add_error(None, THROTTLED_MESSAGE)
            return render(request, 'products/contact_us.html', self._context(request, form), status=429)

        data = form.cleaned_data
        if form.verified:
            sender = {
                'name': profile_full_name(request.user) or request.user.phone_number,
                'phone': request.user.phone_number,
                'email': request.user.email or '',
            }
        else:
            sender = {'name': data['name'], 'phone': data['phone'], 'email': data['email']}
        contact_message = ContactMessage.objects.create(
            name=sender['name'], phone=sender['phone'], email=sender['email'],
            subject=data['subject'], message=data['message'],
            user=request.user if request.user.is_authenticated else None,
            ip_address=ip if ip != 'unknown' else None,
        )
        # شکست یک شنونده (مثلاً اعلان پیامکی) نباید ثبت پیام و پاسخ موفق به کاربر را خراب کند
        for receiver, response in contact_message_received.send_robust(sender=ContactMessage, message=contact_message):
            if isinstance(response, Exception):
                logger.error('شنونده‌ی %r برای پیام تماس %s شکست خورد: %s', receiver, contact_message.pk, response)

        messages.success(request, SUCCESS_MESSAGE)
        return redirect(reverse('products:contact_us'))
