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


@register.filter
def has_kind(points, kind):
    """ آیا در بین نقاط قوت/ضعف یک نظر، حداقل یکی از نوع kind وجود دارد (برای نمایش شرطی ستون جدول) """
    return any(p.kind == kind for p in points.all())
