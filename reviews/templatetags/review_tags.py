from django import template

register = template.Library()


@register.filter
def star_range(rating):
    """ لیستی از ۵ بولین (پر/خالی) برای رسم ردیف ستاره‌ها، بر اساس امتیاز رند شده """
    try:
        rounded = round(float(rating or 0))
    except (TypeError, ValueError):
        rounded = 0
    return [i <= rounded for i in range(1, 6)]
