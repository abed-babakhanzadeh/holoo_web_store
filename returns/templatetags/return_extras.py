"""
فقط یک فیلتر رندری کوچک: چون فیلدهای فرم‌های ویزارد مرجوعی داینامیک‌اند (یک فیلد به‌ازای هر
OrderItem، مثل quantity_12)، جنگو تمپلیت نمی‌تواند با نام ثابت به آن‌ها دسترسی پیدا کند.
هیچ منطق دامنه‌ای/محاسباتی این‌جا نیست - فقط form[field_name] با سینتکس فیلتر.
"""

from django import template

register = template.Library()


@register.filter
def get_form_field(form, field_name):
    return form[field_name]
