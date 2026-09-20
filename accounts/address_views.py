"""
صفحه‌های مدیریت آدرس در پنل کاربری: لیست، افزودن، ویرایش، حذف و تنظیم پیش‌فرض.

همه‌ی آدرس‌ها همیشه با فیلتر مالک (user=request.user) گرفته می‌شوند؛ آدرسِ کاربر دیگر ۴۰۴ می‌دهد.
منطق «پیش‌فرض» (تنها یک پیش‌فرض، جانشین‌شدن هنگام حذف، قفل هم‌زمانی) داخل خودِ مدل Address است؛ ویوها
فقط save()/delete()/set_default() را صدا می‌زنند.
"""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View

from locations.models import DeliveryZone

from .address_forms import AddressForm
from .models import Address


def safe_next_url(request):
    """
    آدرسِ بازگشت (?next=) مثلاً صفحه‌ی تسویه‌حساب؛ فقط مسیرهای همین سایت پذیرفته می‌شود تا کسی با ساختن لینک
    نتواند کاربر را بعد از ثبت آدرس به سایت دیگری بفرستد (Open Redirect). نامعتبر ← رشته‌ی خالی.
    """
    raw = request.GET.get('next') or ''
    if raw and url_has_allowed_host_and_scheme(raw, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return raw
    return ''


def _with_query_param(url, key, value):
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query.setdefault(key, str(value))
    return urlunsplit(parts._replace(query=urlencode(query)))


class AddressListView(LoginRequiredMixin, View):
    template_name = 'accounts/addresses/list.html'

    def get(self, request, *args, **kwargs):
        addresses = list(request.user.addresses.select_related('city', 'city__province', 'zone'))
        # آدرسِ شهرِ ناحیه‌دار که هنوز ناحیه ندارد (مثلاً منتقل‌شده از ساختار قدیمی) باید تکمیل شود
        zoned_city_ids = set(
            DeliveryZone.objects.filter(is_active=True, city_id__in={a.city_id for a in addresses})
            .values_list('city_id', flat=True)
        )
        for address in addresses:
            address.needs_zone = address.zone_id is None and address.city_id in zoned_city_ids
        return render(request, self.template_name, {'active_nav': 'addresses', 'addresses': addresses})


class _AddressFormView(LoginRequiredMixin, View):
    template_name = 'accounts/addresses/form.html'
    success_message = ''

    def get_instance(self, request):
        raise NotImplementedError

    def render_form(self, request, form, instance):
        # آدرسی که شهرش ناحیه‌دار شده ولی ناحیه ندارد: کاربر باید صریحاً ناحیه را انتخاب کند
        needs_zone = bool(instance.pk and instance.zone_id is None and instance.city_id
                          and instance.city.active_zones().exists() and not form.is_bound)
        return render(request, self.template_name, {
            'active_nav': 'addresses', 'form': form, 'address': instance if instance.pk else None,
            'needs_zone': needs_zone,
            'back_url': safe_next_url(request) or reverse('accounts:address_list'),
        })

    def get(self, request, *args, **kwargs):
        instance = self.get_instance(request, **kwargs)
        return self.render_form(request, AddressForm(instance=instance, user=request.user), instance)

    def post(self, request, *args, **kwargs):
        instance = self.get_instance(request, **kwargs)
        form = AddressForm(request.POST, instance=instance, user=request.user)
        if not form.is_valid():
            return self.render_form(request, form, instance)
        address = form.save()
        messages.success(request, self.success_message)
        next_url = safe_next_url(request)
        if next_url:
            # برگشت به صفحه‌ی مبدأ (مثلاً تسویه‌حساب) با آدرسِ تازه/ویرایش‌شده پیش‌انتخاب
            return redirect(_with_query_param(next_url, 'address', address.pk))
        return redirect('accounts:address_list')


class AddressCreateView(_AddressFormView):
    success_message = 'آدرس جدید ثبت شد.'

    def get_instance(self, request, **kwargs):
        return Address(user=request.user)


class AddressEditView(_AddressFormView):
    success_message = 'تغییرات آدرس ذخیره شد.'

    def get_instance(self, request, pk, **kwargs):
        return get_object_or_404(Address.objects.select_related('city', 'city__province', 'zone'), pk=pk, user=request.user)


class AddressDeleteView(LoginRequiredMixin, View):
    http_method_names = ['post']

    def post(self, request, pk, *args, **kwargs):
        address = get_object_or_404(Address, pk=pk, user=request.user)
        was_default, title = address.is_default, address.title
        address.delete()
        messages.success(request, f'آدرس «{title}» حذف شد.')
        if was_default:
            successor = request.user.addresses.filter(is_default=True).first()
            if successor:
                messages.info(request, f'آدرس «{successor.title}» به‌عنوان آدرس پیش‌فرض تنظیم شد.')
        return redirect('accounts:address_list')


class AddressSetDefaultView(LoginRequiredMixin, View):
    http_method_names = ['post']

    def post(self, request, pk, *args, **kwargs):
        address = get_object_or_404(Address, pk=pk, user=request.user)
        address.set_default()
        messages.success(request, f'آدرس «{address.title}» آدرس پیش‌فرض شد.')
        return redirect('accounts:address_list')
