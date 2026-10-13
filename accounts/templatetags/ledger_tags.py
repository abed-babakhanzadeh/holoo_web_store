from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


@register.filter
def money(value):
    """ 1067628949 ← «1,067,628,949» (ارقام لاتین؛ فونت سایت آن‌ها را فارسی نشان می‌دهد) """
    try:
        return f'{int(Decimal(str(value)).quantize(Decimal("1"))):,}'
    except (InvalidOperation, ValueError, TypeError):
        return '0'
