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

from django.db import transaction

# ستون‌هایی که هلو مالکشان است (به‌علاوه‌ی نام برای جستجو و زمان تغییر که با نام/ذخیره عوض می‌شوند)
PRICE_TIER_FIELDS = tuple(f'price{i}' for i in range(2, 11))
HOLOO_OWNED_FIELDS = ('name', 'name_normalized', 'product_code', 'price', *PRICE_TIER_FIELDS, 'stock', 'is_active',
                      'stock_synced_at', 'price_synced_at', 'updated_at')


def safe_float(value, default=0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def sellable_stock(item):
    """
    موجودی قابل‌فروش از ردیف هلو = min(Few, FewSpd)، حداقل ۰.

    Few موجودی فیزیکی است و FewSpd «موجودیِ منهای سفارش‌ها و پیش‌فاکتورهای بازِ هلو» (آزمایش میدانی Holoo2). از وقتی در تنظیمات
    هلو «در ثبت فاکتور، پیش‌فاکتورهای تبدیل‌نشده در موجودی محاسبه شوند» روشن شد، هلو فاکتوری که از FewSpd بیشتر باشد را با
    خطای ۲۸ رد می‌کند (آزمایش: Few=37 و FewSpd=35 ← فروش ۳۶ رد، ۳۵ پذیرفته). پس سقف واقعیِ فاکتورِ سایت FewSpd است و اگر سایت
    تا Few بفروشد، فاکتورِ سفارش‌های پرداخت‌شده در هلو رد می‌شد. بدون FewSpd در پاسخ (نسخه‌ی قدیمی/Mock) همان Few.
    """
    few = safe_float(item.get('Few'))
    spd = item.get('FewSpd')
    stock = few if spd is None else min(few, safe_float(spd, few))
    return max(stock, 0.0)


def row_values(item):
    """ مقدارهای قابل‌نوشتن (ستون‌های مالک هلو) از یک ردیف خام هلو؛ قیمت‌ها به نزدیک‌ترین تومان گرد می‌شوند (ستون صحیح است) """
    values = {
        'price': safe_float(item.get('SellPrice')),
        'stock': sellable_stock(item),
        'product_code': item.get('Code'),
    }
    values.update({f'price{i}': safe_float(item.get(f'SellPrice{i}')) for i in range(2, 11)})
    return values


def _lock_newer_than(product, observed_at):
    """
    ردیف کالا را قفل می‌کند (باید داخل transaction.atomic باشد) و زمان آخرین سینکِ *همان لحظه‌ی دیتابیس* را می‌خواند.
    False = کالا دیگر نیست یا داده‌ی جدیدتری از observed_at نوشته شده (نباید نوشت)؛ True = اجازه‌ی نوشتن.
    بررسی روی شیء داخل حافظه کافی نبود: بین خواندن و نوشتن، واکشیِ هم‌زمانِ دیگری می‌توانست داده‌ی جدیدتر بنویسد و داده‌ی
    قدیمی‌ترِ این مسیر آن را برگرداند (فروش بیش از موجودی).
    """
    row = type(product).objects.select_for_update().filter(pk=product.pk).values('stock_synced_at').first()
    if row is None:
        return False
    return not (row['stock_synced_at'] and row['stock_synced_at'] > observed_at)


def apply_holoo_product_state(product, item, observed_at, source='sync'):
    """
    وضعیت (موجودی/قیمت/نام/کد) محصولِ موجود را از ردیف هلو می‌نشاند. False اگر داده‌ی همین کالا قبلاً از واکشیِ
    جدیدتری نوشته شده بود (چیزی نوشته نشد)، وگرنه True. دسته‌بندی و اسلاگ عمداً دست‌نخورده می‌مانند.
    بررسی «جدیدتر» زیر قفل ردیف و روی مقدار دیتابیس است (اتمیک با نوشتن).
    """
    with transaction.atomic():
        if not _lock_newer_than(product, observed_at):
            return False
        product.name = item.get('Name') or product.erp_code
        for field, value in row_values(item).items():
            setattr(product, field, value)
        product.is_active = True
        product.stock_synced_at = product.price_synced_at = observed_at
        product.save(update_fields=list(HOLOO_OWNED_FIELDS))
        # رزرو مالکِ دیگری دارد و ممکن است هم‌زمان عوض شده باشد؛ تصمیم‌های بعدیِ فراخواننده (مثلاً «موجود شد») با عدد تازه باشد
        product.refresh_from_db(fields=['reserved_quantity'])
    return True


def deactivate_holoo_product(product, observed_at):
    """
    هلو صراحتاً گفته کالا غیرفعال است (IsActive=False در پاسخ معتبر): کالا در سایت غیرفعال و موجودی‌اش صفر می‌شود تا تا
    سینک بعدی (که کالا را پاک می‌کند) قابل خرید نماند. مثل apply_holoo_product_state زیر قفل و با محافظ «داده‌ی جدیدتر».
    """
    with transaction.atomic():
        if not _lock_newer_than(product, observed_at):
            return False
        product.is_active = False
        product.stock = 0
        product.stock_synced_at = observed_at
        product.save(update_fields=['is_active', 'stock', 'stock_synced_at', 'updated_at'])
    return True
