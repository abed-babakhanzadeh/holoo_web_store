from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django import template

register = template.Library()


@register.filter
def money(value):
    """
    مبلغ تومان با جداکننده‌ی هزارگان: 1164760 → 1,164,760. ورودی Decimal/int/float/رشته؛ مقدار خالی یا نامعتبر رشته‌ی خالی می‌دهد.
    (floatformat:"0g" جنگو برای زبان fa گروه‌بندی نمی‌کند، پس فیلتر اختصاصی پروژه است.)
    """
    if value is None or value == '':
        return ''
    try:
        amount = Decimal(str(value)).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return ''
    return f'{int(amount):,}'
