"""
تحلیل فقط‌خواندنیِ داده‌ی کالاهای هلو (تابع‌های خالص، بدون شبکه و بدون دیتابیس).

هدف: پیش از دست زدن به سینک، ساختار و کیفیت واقعی داده‌ی هلو دیده شود؛ مثلاً فیلدهای موجودی (Few / FewSpd / FewTak /
FewKarton) یک چیزند یا نه، قیمت‌ها اعشار دارند یا نه (ستون سایت صحیح است)، کالای خدماتی کدام‌اند و ... .
خروجی یک دیکشنری ساده است تا هم فرمان `check_holoo` چاپش کند هم تست بررسی‌اش کند.
"""

from collections import Counter

from .tasks import classify_item, is_service_item

STOCK_FIELDS = ('Few', 'FewSpd', 'FewTak', 'FewKarton')
PRICE_FIELDS = ['SellPrice'] + [f'SellPrice{i}' for i in range(2, 11)]
# حدود ستون‌های سایت (products.Product)
LIMITS = {'Name': 255, 'Code': 50, 'ErpCode': 100}


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sample(items, limit=3):
    return [{'Code': i.get('Code'), 'Name': i.get('Name'), 'ErpCode': i.get('ErpCode')} for i in items[:limit]]


def analyze_products(items):
    """ تحلیل فهرست خام کالاها (همان عناصر کلید «product» پاسخ هلو) """
    items = list(items)
    result = {'total': len(items)}

    verdicts = Counter(classify_item(i) for i in items)
    result['verdicts'] = dict(verdicts)
    ok_items = [i for i in items if classify_item(i) == 'ok']
    result['importable'] = len(ok_items)

    # --- ساختار فیلدها ---
    key_counts = Counter(k for i in items for k in i)
    result['fields'] = {
        key: {'present': count, 'types': dict(Counter(type(i[key]).__name__ for i in items if key in i))}
        for key, count in sorted(key_counts.items())
    }

    # --- هویت ---
    erp_counts = Counter(i.get('ErpCode') for i in items if i.get('ErpCode'))
    code_groups = {}
    for i in items:
        if i.get('Code'):
            code_groups.setdefault(i['Code'], []).append(i)
    result['identity'] = {
        'duplicate_erp_codes': [code for code, n in erp_counts.items() if n > 1],
        'without_code': len([i for i in items if not i.get('Code')]),
        'duplicate_codes': {code: len(group) for code, group in code_groups.items() if len(group) > 1},
        'over_limit': {
            field: len([i for i in items if len(str(i.get(field) or '')) > limit]) for field, limit in LIMITS.items()
        },
    }

    # --- موجودی ---
    stock = {'few_positive': 0, 'few_zero': 0, 'few_negative': 0, 'few_fractional': 0, 'few_max': 0}
    for i in ok_items:
        few = _num(i.get('Few'))
        if few is None:
            continue
        stock['few_positive'] += few > 0
        stock['few_zero'] += few == 0
        stock['few_negative'] += few < 0
        stock['few_fractional'] += few != int(few)
        stock['few_max'] = max(stock['few_max'], few)
    stock['differs_from'] = {}
    for other in STOCK_FIELDS[1:]:
        differing = [i for i in ok_items if other in i and _num(i.get(other)) != _num(i.get('Few'))]
        stock['differs_from'][other] = {
            'count': len(differing),
            'sample': [{'Name': i.get('Name'), 'Few': i.get('Few'), other: i.get(other)} for i in differing[:3]],
        }
    stock['missing'] = {f: len([i for i in ok_items if f not in i]) for f in STOCK_FIELDS}
    result['stock'] = stock

    # --- قیمت ---
    prices = {
        'sell_zero_among_importable': len([i for i in ok_items if not _num(i.get('SellPrice'))]),
        'negative': 0,
        'fractional': {},
        'tier_nonzero': {},
    }
    for field in PRICE_FIELDS:
        values = [_num(i.get(field)) for i in ok_items if _num(i.get(field)) is not None]
        prices['tier_nonzero'][field] = len([v for v in values if v])
        prices['fractional'][field] = len([v for v in values if v != int(v)])
        prices['negative'] += len([v for v in values if v < 0])
    prices['buy_fractional_examples'] = [i.get('BuyPrice') for i in items if _num(i.get('BuyPrice')) not in (None, int(_num(i.get('BuyPrice')) or 0))][:3]
    result['prices'] = prices

    # --- تخفیف/واحد/تاریخ/گروه ---
    result['discount'] = {
        'with_percent_or_price': len([i for i in items if _num(i.get('DiscountPercent')) or _num(i.get('DiscountPrice'))]),
        'with_validity_dates': len([i for i in items if str(i.get('EtebarTakhfifAz') or '').strip() or str(i.get('EtebarTakhfifTa') or '').strip()]),
    }
    result['units'] = dict(Counter(str(i.get('UnitErpCode')) for i in items))
    dates = sorted(str(i['modifyDate']) for i in items if i.get('modifyDate'))
    result['modify_date'] = {'present': len(dates), 'min': dates[0] if dates else None, 'max': dates[-1] if dates else None}
    result['groups'] = {
        'main': len({i.get('MainGroupErpCode') for i in items if i.get('MainGroupErpCode')}),
        'side': len({i.get('SideGroupErpCode') for i in items if i.get('SideGroupErpCode')}),
        'without_main': len([i for i in ok_items if not i.get('MainGroupErpCode')]),
    }
    result['more_codes'] = len([i for i in items if i.get('MoreCodes') and i['MoreCodes'] != ['null']])

    # --- فهرست‌های نمونه برای تصمیم ---
    services = [i for i in items if is_service_item(i)]
    result['services'] = [
        {'Code': i.get('Code'), 'Name': i.get('Name'), 'ErpCode': i.get('ErpCode'), 'Few': i.get('Few'),
         'SellPrice': i.get('SellPrice'), 'IsActive': i.get('IsActive')} for i in services
    ]
    result['inactive_with_stock'] = len([i for i in items if classify_item(i) == 'inactive' and (_num(i.get('Few')) or 0) > 0])
    result['samples'] = {'excluded_name': _sample([i for i in items if classify_item(i) == 'excluded_name'])}
    return result


def compare_with_site(items, site_erp_codes):
    """
    مقایسه‌ی کالاهای هلو با کالاهای فعلی دیتابیس سایت (فقط ErpCodeها، بدون خواندن چیز دیگری).
    «would_be_deleted» دقیقاً همان چیزی است که مرحله‌ی پاک‌سازی سینک اگر امروز اجرا شود حذف می‌کند.
    """
    site = {c for c in site_erp_codes if c}
    importable = {i['ErpCode'] for i in items if classify_item(i) == 'ok'}
    any_holoo = {i['ErpCode'] for i in items if i.get('ErpCode')}
    return {
        'site_total': len(site),
        'matched': len(site & importable),
        'new_in_holoo': len(importable - site),
        'would_be_deleted': len(site - importable),
        'would_be_deleted_but_exist_in_holoo_filtered': len((site - importable) & any_holoo),
        'would_be_deleted_missing_from_holoo': len(site - any_holoo),
    }
