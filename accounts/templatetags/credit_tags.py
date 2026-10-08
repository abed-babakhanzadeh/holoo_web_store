from django import template

register = template.Library()


@register.simple_tag
def show_cheque_credit_nav(user):
    """
    آیتم «درخواست خرید چکی» در منوی پنل فقط برای کسی که می‌تواند درخواست بدهد (مشتری نقدی، یا ویژه با سیاست «درخواست») یا
    درخواستی دارد نمایش داده می‌شود؛ مشتری چکی و کسی که مجوز فعال دارد نیازی به آن ندارد. بدون کوئری اضافه (تنظیمات سایت کش است).
    """
    if user is None or not getattr(user, 'is_authenticated', False) or not user.can_order():
        return False
    from orders import payment_options                # وارد کردن دیرهنگام: accounts به orders وابسته نشود
    return payment_options.request_option(user) is not None
