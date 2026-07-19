from django import template

register = template.Library()


@register.simple_tag
def is_comparing(product, request):
    """ آیا این محصول در حال حاضر در لیست مقایسه‌ی سشن کاربر است """
    return product.id in request.session.get('compare_ids', [])
