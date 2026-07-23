import re

# نگاشت حروف/ارقام عربی به معادل فارسی/لاتینشان، طبق نقطه‌کد یونیکد (نه گلیف)، تا از هر گونه
# اشتباه دیداری بین حروف عربی و فارسیِ شبیه‌به‌هم (مثلاً ي/ی یا ك/ک) در سورس جلوگیری شود.
_CHAR_MAP = {
    chr(0x064A): chr(0x06CC),  # ي (Arabic Yeh) -> ی (Persian Yeh)
    chr(0x0649): chr(0x06CC),  # ى (Alef Maksura) -> ی
    chr(0x0643): chr(0x06A9),  # ك (Arabic Kaf) -> ک (Persian Keheh)
    chr(0x0629): chr(0x0647),  # ة (Teh Marbuta) -> ه (Heh)
    chr(0x0623): chr(0x0627),  # أ (Alef+Hamza above) -> ا (Alef)
    chr(0x0625): chr(0x0627),  # إ (Alef+Hamza below) -> ا
    chr(0x0671): chr(0x0627),  # ٱ (Alef Wasla) -> ا
    chr(0x200C): ' ',          # نیم‌فاصله (ZWNJ) -> فاصله‌ی معمولی
}
# ارقام عربی (٠-٩ = U+0660..U+0669) و فارسی (۰-۹ = U+06F0..U+06F9) -> ارقام لاتین
for _i in range(10):
    _CHAR_MAP[chr(0x0660 + _i)] = str(_i)
    _CHAR_MAP[chr(0x06F0 + _i)] = str(_i)

_TRANSLATION = str.maketrans(_CHAR_MAP)

# اعراب: فتحه‌تنوین..سکون (U+064B..U+0652)، الف کوچک بالانویس (U+0670)، کشیده/تطویل (U+0640)
_DIACRITIC_CODEPOINTS = list(range(0x064B, 0x0653)) + [0x0670, 0x0640]
_DIACRITICS_RE = re.compile('[' + ''.join(chr(c) for c in _DIACRITIC_CODEPOINTS) + ']')
_WHITESPACE_RE = re.compile(r'\s+')


def normalize_persian(text: str) -> str:
    """
    برای جستجوی فارسی/عربی: حروف مشابه عربی/فارسی (ي/ی، ك/ک و ...)، اعداد عربی/فارسی، نیم‌فاصله
    و اعراب را یکدست می‌کند تا نتیجه‌ی جستجو به تفاوت رسم‌الخط ورودی کاربر حساس نباشد. برای مقایسه
    باید هم مقدار ذخیره‌شده و هم عبارت جستجو با همین تابع نرمال شوند.
    """
    if not text:
        return ''
    text = text.translate(_TRANSLATION)
    text = _DIACRITICS_RE.sub('', text)
    text = _WHITESPACE_RE.sub(' ', text).strip()
    return text.lower()
