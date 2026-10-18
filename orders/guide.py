"""
صفحه‌های «راهنمای خرید»: نحوه ثبت سفارش، رویه ارسال سفارش و شیوه‌های پرداخت (صفحه‌ی رویه بازگرداندن کالا در returns است و از همین قالب
مشترک templates/orders/guide/base.html استفاده می‌کند).

متن‌ها فقط چیزهایی را می‌گویند که واقعاً در سایت پیاده شده است؛ هر عددی که در تنظیمات/دیتابیس هست (مهلت چک، مهلت مرجوعی، نواحی و تعرفه‌ی
پیک، قاعده‌های ارسال رایگان، پس‌کرایه) زنده خوانده می‌شود تا با تغییر ادمین، صفحه کهنه نماند.
"""

from django.utils import timezone
from django.views.generic import TemplateView

from locations.models import City, DeliveryZone, Province
from products.models import SiteSettings
from products.stock import RESERVATION_TTL
from promotions import free_shipping


def return_period_text(settings_obj):
    """ «۷ روز کاری» / «۷ روز تقویمی» برای نمایش در متن صفحه‌ها """
    unit = 'روز کاری' if settings_obj.return_period_unit == SiteSettings.RETURN_PERIOD_UNIT_WORKING_DAYS else 'روز تقویمی'
    return f'{settings_obj.return_period_days} {unit}'


class GuidePage(TemplateView):
    guide_name = ''
    guide_current = ''

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        settings_obj = SiteSettings.cached()
        context.update({
            'guide_name': self.guide_name, 'guide_current': self.guide_current,
            'return_period': return_period_text(settings_obj),
        })
        return context


class HowToOrderView(GuidePage):
    template_name = 'orders/guide/how_to_order.html'
    guide_name = 'نحوه ثبت سفارش'
    guide_current = 'order'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        settings_obj = SiteSettings.cached()
        context.update({
            'reservation_minutes': int(RESERVATION_TTL.total_seconds() // 60),
            'cheque_hours': settings_obj.cheque_submission_deadline_hours,
        })
        return context


def free_rule_rows():
    """ قاعده‌های ارسال رایگانِ فعال برای نمایش: [{'title', 'min_total', 'scope', 'covers_postage'}] (کلید سراسری خاموش ← خالی) """
    rules = [rule for rule in free_shipping.enabled_rules() if rule.in_window(timezone.now())]
    cities = {c.pk: c.name for c in City.objects.filter(pk__in={cid for r in rules for cid in r.city_ids})}
    provinces = {p.pk: p.name for p in Province.objects.filter(pk__in={pid for r in rules for pid in r.province_ids})}
    rows = []
    for rule in rules:
        if rule.nationwide:
            scope = 'سراسر کشور'
        else:
            scope = '، '.join([provinces[pid] for pid in sorted(rule.province_ids) if pid in provinces]
                              + [cities[cid] for cid in sorted(rule.city_ids) if cid in cities])
        rows.append({'title': rule.title, 'min_total': rule.min_total, 'scope': scope, 'covers_postage': rule.covers_postage})
    return rows


class ShippingView(GuidePage):
    template_name = 'orders/guide/shipping.html'
    guide_name = 'رویه ارسال سفارش'
    guide_current = 'shipping'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        settings_obj = SiteSettings.cached()
        zones = DeliveryZone.objects.filter(is_active=True, shipping_cost__gt=0).select_related('city')
        by_city = {}
        for zone in zones:
            by_city.setdefault(zone.city, []).append(zone)
        context.update({
            'courier_cities': [{'city': city, 'zones': items} for city, items in by_city.items()],
            'courier_city_names': '، '.join(city.name for city in by_city),
            'postage_enabled': settings_obj.postage_collect_enabled,
            'postage_label': settings_obj.postage_collect_label,
            'free_rules': free_rule_rows(),
        })
        return context


class PaymentView(GuidePage):
    template_name = 'orders/guide/payment.html'
    guide_name = 'شیوه‌های پرداخت'
    guide_current = 'payment'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        settings_obj = SiteSettings.cached()
        context.update({
            'reservation_minutes': int(RESERVATION_TTL.total_seconds() // 60),
            'cheque_hours': settings_obj.cheque_submission_deadline_hours,
        })
        return context
