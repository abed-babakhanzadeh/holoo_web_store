"""
قطعه‌های HTML برای کمبوهای وابسته‌ی آدرس (htmx): استان ← شهر ← ناحیه.

فقط برای کاربر واردشده؛ کاربر ناشناس ۴۰۱ می‌گیرد (نه ریدایرکت به صفحه‌ی ورود، چون htmx آن صفحه‌ی کامل
را داخل کمبو می‌ریخت).
"""

from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from .models import City, DeliveryZone


def _int(value):
    return int(value) if value and str(value).isdigit() else None


@require_GET
def city_options(request):
    """ گزینه‌های شهرهای فعالِ یک استان + خالی‌کردن کمبوی ناحیه (تغییر استان یعنی شهر و ناحیه از نو انتخاب شود) """
    if not request.user.is_authenticated:
        return HttpResponse(status=401)
    province_id = _int(request.GET.get('province'))
    cities = City.objects.filter(province_id=province_id, is_active=True).order_by('name') if province_id else City.objects.none()
    return render(request, 'locations/city_options.html', {'cities': cities})


@require_GET
def zone_field(request):
    """ کمبوی ناحیه‌ی یک شهر؛ اگر شهر ناحیه‌ی فعال ندارد یک div خالی برمی‌گردد (و کمبو ناپدید می‌شود) """
    if not request.user.is_authenticated:
        return HttpResponse(status=401)
    city_id = _int(request.GET.get('city'))
    zones = DeliveryZone.objects.filter(city_id=city_id, is_active=True).order_by('sort_order', 'name') if city_id else DeliveryZone.objects.none()
    return render(request, 'locations/zone_field.html', {'zones': zones, 'selected': ''})
