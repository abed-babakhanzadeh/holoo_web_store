"""
اجزای مشترک فاکتور سفارش و صورت‌حساب برگشت از فروش: مشخصات فروشنده (با سوییچ‌های سربرگ) و بارکد.
"""
from django.utils.html import format_html
from django.utils.safestring import mark_safe


def seller_details(site_settings):
    """
    سربرگ فروشنده از SiteSettings.
    قاعده: هر مورد سربرگ (نام حقوقی، شناسه ملی، شماره ثبت، کد اقتصادی) فقط وقتی برگردانده می‌شود که هم سوییچ
    invoice_show_* آن روشن باشد و هم مقدارش خالی نباشد. نام فروشنده اگر نام حقوقی چاپ نشود به نام تجاری برمی‌گردد
    (فاکتور بدون نام فروشنده بی‌معنی است). آدرس، کد پستی و تلفن سوییچ ندارند و اگر پر باشند چاپ می‌شوند.
    مهر فقط با روشن بودن invoice_show_stamp و بارگذاری تصویر برمی‌گردد.
    """
    s = site_settings

    def shown(switch, value):
        value = (value or '').strip()
        return value if switch and value else ''

    legal_name = shown(s.invoice_show_legal_name, s.store_legal_name)
    return {
        'name': legal_name or (s.store_name or '').strip(),
        'national_id': shown(s.invoice_show_national_id, s.store_national_id),
        'registration_number': shown(s.invoice_show_registration_number, s.store_registration_number),
        'economic_code': shown(s.invoice_show_economic_code, s.store_economic_code),
        'address': (s.store_address or '').strip(),
        'postal_code': (s.store_postal_code or '').strip(),
        'phone': (s.store_phone_1 or s.store_phone_2 or s.store_mobile or '').strip(),
        'stamp_url': s.store_stamp_image.url if (s.invoice_show_stamp and s.store_stamp_image) else '',
    }


# الگوی Code 39 برای ارقام و کاراکتر شروع/پایان «*»: ۹ المان (بار، فاصله، بار، ...) که w = پهن و n = باریک است
_CODE39 = {
    '0': 'nnnwwnwnn', '1': 'wnnwnnnnw', '2': 'nnwwnnnnw', '3': 'wnwwnnnnn', '4': 'nnnwwnnnw',
    '5': 'wnnwwnnnn', '6': 'nnwwwnnnn', '7': 'nnnwnnwnw', '8': 'wnnwnnwnn', '9': 'nnwwnnwnn',
    '*': 'nwnnwnwnn',
}


def code39_svg(digits, height=46):
    """ بارکد Code 39 (فقط ارقام) به‌صورت SVG درون‌خطی؛ بدون وابستگی به کتابخانه‌ی خارجی. ورودی غیررقمی ValueError می‌دهد """
    text = str(digits)
    if not text.isdigit():
        raise ValueError('بارکد فقط ارقام می‌پذیرد.')
    narrow, wide, gap = 1, 3, 1
    x = 0
    rects = []
    for index, char in enumerate(f'*{text}*'):
        if index:
            x += gap                                             # فاصله‌ی بین دو کاراکتر
        for element, width_flag in enumerate(_CODE39[char]):
            width = wide if width_flag == 'w' else narrow
            if element % 2 == 0:                                 # عناصر زوج بار هستند، فرد فاصله
                rects.append(f'<rect x="{x}" y="0" width="{width}" height="{height}"/>')
            x += width
    svg = format_html(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {} {}" preserveAspectRatio="none" role="img" '
        'aria-label="بارکد {}" class="inv-barcode-svg" fill="currentColor">{}</svg>',
        x, height, text, mark_safe(''.join(rects)),
    )
    return svg
