"""
جستجوی نام محصول: «همه‌ی کلمه‌ها، در هر جای نام و به هر ترتیب».

عبارت جستجو به کلمه‌ها شکسته می‌شود (پس از یکدست‌سازی ی/ک، ارقام، نیم‌فاصله و اعراب با normalize_persian) و محصول وقتی می‌آید که
*همه‌ی* کلمه‌ها در نامش باشند، نه فقط یکی: «کرم سانی» ← «کرم آبرسانی ۱۰۰ میلی‌گرم سانی رنگ قرمز»، ولی نه هر کالایی که فقط «کرم» دارد.
هر کلمه زیررشته‌ی نام است (icontains)، پس «سانی» داخل «آبرسانی» هم پیدا می‌شود؛ همان رفتار قبلیِ جستجوی تک‌کلمه‌ای. هر دو جستجوی
سایت (کادر زنده‌ی هدر و صفحه‌ی فروشگاه) از همین تابع استفاده می‌کنند تا نتیجه‌ی کادر و صفحه همیشه یکی باشد.
"""
from services.text import normalize_persian

MAX_TERMS = 8                 # سقف کلمه‌ها؛ هر کلمه یک شرط LIKE است و ورودی بلندِ بدخواهانه نباید کوئری را سنگین کند


def search_terms(query):
    """ کلمه‌های یکدست‌شده‌ی عبارت (حداکثر MAX_TERMS، بدون تکرار با حفظ ترتیب) """
    seen = []
    for term in normalize_persian(query or '').split(' '):
        if term and term not in seen:
            seen.append(term)
    return seen[:MAX_TERMS]


def filter_by_name(queryset, query):
    """ محصولاتی که نامشان همه‌ی کلمه‌های query را دارد؛ عبارت بدون کلمه (خالی/فقط فاصله) ← هیچ نتیجه‌ای """
    terms = search_terms(query)
    if not terms:
        return queryset.none()
    for term in terms:
        queryset = queryset.filter(name_normalized__icontains=term)
    return queryset
