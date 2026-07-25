import jdatetime
from django import template
from django.utils import timezone

register = template.Library()

# ماه‌های شمسی رو خودمون نگه می‌داریم (نه از locale سیستم‌عامل)، چون نام لوکیل fa_IR بین
# ویندوز ('Persian_Iran') و لینوکس ('fa_IR') فرق داره و jdatetime.set_locale به‌صورت پرتابل کار نمی‌کنه
PERSIAN_MONTHS = [
    'فروردین', 'اردیبهشت', 'خرداد', 'تیر', 'مرداد', 'شهریور',
    'مهر', 'آبان', 'آذر', 'دی', 'بهمن', 'اسفند',
]


@register.filter(name='jalali')
def jalali(value, fmt='%Y/%m/%d'):
    """ تبدیل datetime میلادی به رشته‌ی تاریخ شمسی؛ %B در fmt با نام فارسی ماه جایگزین می‌شود """
    if not value:
        return ''
    if timezone.is_aware(value):
        value = timezone.localtime(value)
    jd = jdatetime.datetime.fromgregorian(datetime=value)
    fmt = fmt.replace('%B', PERSIAN_MONTHS[jd.month - 1])
    return jd.strftime(fmt)
