from django import template
from django.utils.html import format_html

register = template.Library()

# آدمک مینیمال تو‌پر (currentColor)؛ اندازه‌ی آن را .user-badge-icon در app.css نسبت به دایره می‌گیرد
PERSON_SVG = (
    '<svg class="user-badge-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">'
    '<path d="M7.5 6a4.5 4.5 0 1 1 9 0 4.5 4.5 0 0 1-9 0ZM3.751 20.105a8.25 8.25 0 0 1 16.498 0 .75.75 0 0 1-.437.695A18.683 18.683 0 0 1 12 22.5c-2.786 0-5.433-.608-7.812-1.7a.75.75 0 0 1-.437-.695Z"/>'
    '</svg>'
)


@register.simple_tag
def user_initial(user):
    """
    حرف اول بج آواتار متنی (کاربر لاگین‌شده‌ی بدون عکس): اولین حرف «نام» و در نبودش «نام خانوادگی». فاصله‌های ابتدایی حذف می‌شوند
    و حرف لاتین بزرگ می‌شود. کاربری که فقط شماره‌ی موبایل دارد حرفی ندارد (رشته‌ی خالی)؛ رقم اول موبایل همیشه «0» است و معنایی
    ندارد، پس بج در آن حالت آیکون آدمک می‌گیرد (نگاه کنید user_badge_glyph). هیچ‌وقت خطا نمی‌دهد، حتی برای user خالی/ناشناس.
    """
    for field in ('first_name', 'last_name'):
        value = (getattr(user, field, '') or '').strip()
        if value:
            return value[0].upper()
    return ''


@register.simple_tag
def user_badge_glyph(user):
    """ محتوای داخل دایره‌ی آواتار: حرف اول نام/نام خانوادگی، وگرنه آیکون SVG آدمک (سفید با رنگ متن دایره) """
    initial = user_initial(user)
    return initial if initial else format_html(PERSON_SVG)
