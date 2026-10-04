"""
تنها نقطه‌ی نوشتن «وضعیت کالا از هلو» (موجودی و قیمت) روی Product.

هر مسیری که داده‌ی هلو را می‌آورد (سینک کامل، و در آینده سینک افزایشی، وب‌هوک یا استعلام آنی) باید فقط از این‌جا بنویسد:

  ۱. فقط ستون‌های مالکِ هلو نوشته می‌شوند (save(update_fields=...))؛ reserved_quantity — که مالکش رزرو سفارش‌های سایت
     است — هرگز. قبلاً سینک product.save() کامل می‌زد و رزروی که هم‌زمان ثبت شده بود با نسخه‌ی قدیمیِ حافظه
     بازنویسی می‌شد (lost update).
  ۲. هر نوشتن برچسب observed_at دارد: لحظه‌ی *شروع* واکشی‌ای که داده را آورد. داده‌ی قدیمی‌تر روی جدیدتر نمی‌نشیند
     (مثلاً صفحه‌ی کُند سینک که بعد از یک استعلام آنی نوشته شود). آزادسازی رزرو فاکتورشده هم به همین برچسب تکیه دارد
     (products/stock.py::release_synced).
"""

# ستون‌هایی که هلو مالکشان است (به‌علاوه‌ی نام برای جستجو و زمان تغییر که با نام/ذخیره عوض می‌شوند)
PRICE_TIER_FIELDS = tuple(f'price{i}' for i in range(2, 11))
HOLOO_OWNED_FIELDS = ('name', 'name_normalized', 'product_code', 'price', *PRICE_TIER_FIELDS, 'stock', 'is_active',
                      'stock_synced_at', 'price_synced_at', 'updated_at')


def safe_float(value, default=0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def row_values(item):
    """ مقدارهای قابل‌نوشتن (ستون‌های مالک هلو) از یک ردیف خام هلو؛ قیمت‌ها به نزدیک‌ترین تومان گرد می‌شوند (ستون صحیح است) """
    values = {
        'price': safe_float(item.get('SellPrice')),
        'stock': safe_float(item.get('Few')),
        'product_code': item.get('Code'),
    }
    values.update({f'price{i}': safe_float(item.get(f'SellPrice{i}')) for i in range(2, 11)})
    return values


def apply_holoo_product_state(product, item, observed_at, source='sync'):
    """
    وضعیت (موجودی/قیمت/نام/کد) محصولِ موجود را از ردیف هلو می‌نشاند. False اگر داده‌ی همین کالا قبلاً از واکشیِ
    جدیدتری نوشته شده بود (چیزی نوشته نشد)، وگرنه True. دسته‌بندی و اسلاگ عمداً دست‌نخورده می‌مانند.
    """
    if product.stock_synced_at and product.stock_synced_at > observed_at:
        return False
    product.name = item.get('Name') or product.erp_code
    for field, value in row_values(item).items():
        setattr(product, field, value)
    product.is_active = True
    product.stock_synced_at = product.price_synced_at = observed_at
    product.save(update_fields=list(HOLOO_OWNED_FIELDS))
    return True
