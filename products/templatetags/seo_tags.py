"""تزریق امنِ داده‌ی ساختاریافته (schema.org / JSON-LD) به قالب‌ها - SEO Phase C."""

import json

from django import template
from django.core.serializers.json import DjangoJSONEncoder
from django.utils.html import format_html
from django.utils.safestring import mark_safe

register = template.Library()

# همان جدول escape چهارکاراکتری که django.utils.html.json_script داخلی استفاده می‌کند (ضدِ
# شکستِ تگ </script> و XSS)؛ اینجا تکرار شده چون json_script خودِ جنگو نوع تگ را ثابت روی
# application/json می‌گذارد، درحالی‌که Google فقط application/ld+json را برای structured data می‌خواند.
_JSONLD_ESCAPES = {
    ord('<'): '\\u003C',
    ord('>'): '\\u003E',
    ord('&'): '\\u0026',
    ord(' '): '\\u2028',
    ord(' '): '\\u2029',
}


@register.simple_tag
def jsonld_script(data):
    """
    یک دیکشنری پایتونی (که ممکن است Decimal/datetime هم داخلش باشد - DjangoJSONEncoder هر دو را
    سریالایز می‌کند) را به‌عنوان <script type="application/ld+json"> امن رندر می‌کند.
    """
    json_str = json.dumps(data, cls=DjangoJSONEncoder).translate(_JSONLD_ESCAPES)
    return format_html('<script type="application/ld+json">{}</script>', mark_safe(json_str))
