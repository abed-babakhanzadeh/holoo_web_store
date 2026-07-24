from .captcha import get_captcha_code, new_captcha


def login_captcha(request):
    """
    چون فرم «ورود با رمز عبور» همیشه (روی همه‌ی صفحات، داخل مودال هدر) رندر می‌شود، کپچای آن هم
    باید همیشه در دسترس باشد. برای جلوگیری از ساختن یک کد تازه روی هر بازدید صفحه، تا وقتی کپچای
    فعلیِ سشن مصرف نشده همان را نگه می‌داریم و فقط در نبودش یکی تازه می‌سازیم.
    """
    if request.user.is_authenticated:
        return {}
    key = request.session.get('login_captcha_key')
    if not key or not get_captcha_code(request, key):
        key = new_captcha(request)
        request.session['login_captcha_key'] = key
    return {'login_captcha_key': key}
