from .captcha import get_captcha_code, new_captcha


def login_captcha(request):
    """
    چون فرم «ورود با رمز عبور» همیشه (روی همه‌ی صفحات، داخل مودال هدر) رندر می‌شود، کپچای آن هم
    باید همیشه در دسترس باشد. برای جلوگیری از ساختن یک کد تازه روی هر بازدید صفحه، تا وقتی کپچای
    فعلیِ سشن مصرف نشده همان را نگه می‌داریم و فقط در نبودش یکی تازه می‌سازیم.
    """
    if request.user.is_authenticated:
        # برای کاربر واردشده کپچا لازم نیست؛ به‌جایش وضعیت تکمیل پروفایل (همین پردازنده‌ی از قبل ثبت‌شده، تا ثبت جدیدی در
        # تنظیمات لازم نباشد)
        return profile_nudge(request)
    key = request.session.get('login_captcha_key')
    if not key or not get_captcha_code(request, key):
        key = new_captcha(request)
        request.session['login_captcha_key'] = key
    return {'login_captcha_key': key}


def profile_nudge(request):
    """
    کاربر واردشده‌ای که پروفایلش ناقص است باید همه‌جا بداند و راه تکمیل را ببیند (نوار بالای سایت و پیشخوان).
    profile_incomplete_imported: مشتریِ قبلی هلو؛ متنش شخصی‌تر است (با تکمیل نام و کدملی فوراً فعال می‌شود).
    """
    user = getattr(request, 'user', None)
    if user is None or not user.is_authenticated or not user.needs_profile_completion:
        return {}
    return {'profile_incomplete': True, 'profile_incomplete_imported': bool(user.imported_from_holoo)}
