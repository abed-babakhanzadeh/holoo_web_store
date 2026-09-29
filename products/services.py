import re
from pathlib import Path

from django.conf import settings

from .models import Product, ProductImage

CATALOG_DIR_NAME = 'products/catalog'
FILENAME_RE = re.compile(r'^(?P<code>.+)-(?P<idx>\d{1,3})\.(?P<ext>jpe?g|png|webp)$', re.IGNORECASE)
MAX_GALLERY_INDEX = 50


def _catalog_dir() -> Path:
    path = Path(settings.MEDIA_ROOT) / CATALOG_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _normalize_numeric_code(code):
    """ برای کدهای صرفاً عددی، صفرهای ابتدایی را حذف می‌کند ('0010041' -> '10041'؛ '000' -> '0').
    کدهای غیرعددی (مثل erp_code) دست‌نخورده برمی‌گردند - این نرمال‌سازی فقط برای «کد کالا»ی
    عددی معنا دارد، چون اشتباه رایج تیم عکاسی همین صفر اضافه/کم در ابتدای نام فایل است. """
    return (code.lstrip('0') or '0') if code.isdigit() else code


def _build_normalized_code_index():
    """
    نگاشت «کد نرمال‌شده (بدون صفر ابتدایی) -> لیست id محصولاتی که product_code شان بعد از
    نرمال‌سازی همین مقدار است». یک‌بار در ابتدای sync_product_images ساخته و به _match_product
    پاس داده می‌شود تا برای هر کدِ تطبیق‌نیافته مجبور به اسکن کل جدول محصولات نباشیم.
    """
    index = {}
    codes = Product.objects.exclude(product_code__isnull=True).exclude(product_code='').values_list('id', 'product_code')
    for product_id, product_code in codes:
        index.setdefault(_normalize_numeric_code(product_code), []).append(product_id)
    return index


def _match_product(code, normalized_index):
    """
    تلاش برای یافتن دقیقاً یک محصول برای کدِ استخراج‌شده از نام فایل:
    ۱. ابتدا با product_code («کد کالا»، فیلد خوانا برای انبار/عکاسی - مثل 00215002 یا 125) - تطبیق دقیق.
    ۲. اگر دقیق چیزی پیدا نشد و کد عددی بود، تطبیق با صفر ابتدایی نادیده‌گرفته‌شده (مثلاً فایل
       «0010041» با محصولی با product_code=«10041» جا می‌افتد) - اشتباه رایج تیم عکاسی، بدون
       نیاز به تغییر دستی نام فایل.
    ۳. اگر باز هم چیزی نبود، Fallback به erp_code (شناسه‌ی هش/بیس۶۴ داخلی هلو) برای سازگاری با
       فایل‌های قدیمی که قبلاً با آن نام‌گذاری شده و هنوز در پوشه‌ی کاتالوگ مانده‌اند.

    خروجی: (Product|None, is_ambiguous). is_ambiguous=True یعنی بیش از یک محصول با همین کد
    (دقیق یا بعد از نرمال‌سازی) وجود دارد - برای جلوگیری از الصاق اشتباه تصویر، فایل باید رد شود،
    نه این‌که به‌صورت غیرقطعی به اولین محصول وصل شود.
    """
    by_code = Product.objects.filter(product_code=code).exclude(product_code__isnull=True).exclude(product_code='')
    count = by_code.count()
    if count > 1:
        return None, True
    if count == 1:
        return by_code.first(), False

    if code.isdigit():
        # هر دو جهت را می‌پوشاند: هم صفر اضافه در نام فایل (مثل '0010041' برای واقعیِ '10041')،
        # هم صفر کم‌افتاده در نام فایل (مثل '215002' برای واقعیِ '00215002') - چون index بر
        # مبنای فرم نرمال‌شده‌ی خودِ product_code هم ساخته شده، نه فقط فرم نرمال‌شده‌ی code ورودی.
        candidate_ids = normalized_index.get(_normalize_numeric_code(code), [])
        if len(candidate_ids) > 1:
            return None, True
        if len(candidate_ids) == 1:
            return Product.objects.filter(id=candidate_ids[0]).first(), False

    return Product.objects.filter(erp_code=code).first(), False


def sync_product_images():
    """
    پوشه‌ی media/products/catalog/ را می‌خواند و فایل‌هایی با نام «{code}-{شماره}.jpg/png/webp» را به
    تصویر اصلی محصول (شماره ۱) یا گالری آن (شماره ۲ به بعد) وصل می‌کند. تطبیق کد با _match_product
    انجام می‌شود: اول product_code («کد کالا») دقیق، بعد همان کد بدون صفر ابتدایی، بعد Fallback
    به erp_code برای فایل‌های قدیمی.

    Idempotent است: اجرای دوباره روی وضعیت بدون‌تغییر کاری انجام نمی‌دهد. عکاس/گرافیست فقط کافیست
    فایل‌ها را با این قرارداد در پوشه کپی کند؛ حذف کردن یک فایل از پوشه هم رکورد متناظرش را پاک می‌کند.
    """
    catalog_dir = _catalog_dir()

    matched = 0
    updated = 0
    unmatched = []
    ambiguous = []
    seen_paths = set()

    groups = {}
    for entry in sorted(catalog_dir.iterdir()):
        if not entry.is_file():
            continue
        m = FILENAME_RE.match(entry.name)
        if not m:
            unmatched.append(entry.name)
            continue
        idx = int(m.group('idx'))
        if idx < 1 or idx > MAX_GALLERY_INDEX:
            unmatched.append(entry.name)
            continue
        groups.setdefault(m.group('code'), {})[idx] = entry.name

    normalized_index = _build_normalized_code_index()

    for code, files_by_idx in groups.items():
        product, is_ambiguous = _match_product(code, normalized_index)
        if is_ambiguous:
            ambiguous.extend(files_by_idx.values())
            continue
        if not product:
            unmatched.extend(files_by_idx.values())
            continue

        for idx, filename in sorted(files_by_idx.items()):
            rel_path = f'{CATALOG_DIR_NAME}/{filename}'
            seen_paths.add(rel_path)
            matched += 1

            if idx == 1:
                if (product.main_image.name or '') != rel_path:
                    product.main_image = rel_path
                    product.save(update_fields=['main_image'])
                    updated += 1
            else:
                image_obj, created = ProductImage.objects.get_or_create(
                    product=product, order=idx, defaults={'image': rel_path},
                )
                if created:
                    updated += 1
                elif image_obj.image.name != rel_path:
                    image_obj.image = rel_path
                    image_obj.save(update_fields=['image'])
                    updated += 1

    pruned = _prune_missing(seen_paths)

    return {
        'matched': matched,
        'updated': updated,
        'pruned': pruned,
        'unmatched': unmatched,
        'ambiguous': ambiguous,
    }


def delete_products_safely(queryset):
    """
    حذف فیزیکی امن یک QuerySet از محصولات (برای کالاهایی که در هلو IsActive=False یا از کاتالوگ
    هلو حذف شده‌اند - سیاست فعلی این است که چنین کالاهایی اصلاً در دیتابیس ما نمانند).

    چرا این تابع لازم است: مدل Review یک FK خودارجاع دارد (Review.parent، برای پاسخ‌ها) با
    on_delete=CASCADE. وقتی جنگو بخواهد یک‌جا چند محصول را که مجموعاً یک رشته‌ی نظر/پاسخِ چندلایه
    (نظر -> پاسخ -> پاسخِ پاسخ) دارند حذف کند، پیش از حذف واقعی، ستون parent_id همه‌ی ردیف‌های آن
    رشته را یک‌جا Null می‌کند (مکانیزم دفاعی خودِ Collector جنگو برای مدل‌های خودارجاع) - این کار
    می‌تواند موقتاً چند ردیف هم‌زمان parent=NULL برای یک (user, product) بسازد و با ایندکس یکتای
    one_top_level_review_per_user_product تداخل کند (IntegrityError در MSSQL). راه‌حل: پیش از حذف
    محصولات، نظرات/پاسخ‌های وابسته را لایه‌به‌لایه از برگ‌ها (پاسخ‌هایی که خودشان پاسخی ندارند) به
    سمت ریشه حذف می‌کنیم؛ در هر مرحله فقط ردیف‌هایی حذف می‌شوند که چیزی به آن‌ها اشاره نمی‌کند، پس
    هرگز نیازی به Null‌کردن دسته‌ای parent_id (و برخورد با ایندکس یکتا) پیش نمی‌آید.

    بقیه‌ی روابط محصول (OrderItem با SET_NULL؛ ProductImage/ProductColor/ProductFeatureValue/
    StockAlert/Wishlist/CartItem/PromotionTarget با CASCADE ساده و بدون خودارجاعی) مشکلی از این
    جنس ندارند و به‌صورت عادی توسط .delete() جنگو مدیریت می‌شوند.

    خروجی: (تعداد محصولات حذف‌شده, تعداد نظرات/پاسخ‌های حذف‌شده)
    """
    from reviews.models import Review

    product_ids = list(queryset.values_list('id', flat=True))
    if not product_ids:
        return 0, 0

    deleted_reviews = 0
    while True:
        scoped = Review.objects.filter(product_id__in=product_ids)
        referenced_parent_ids = set(scoped.exclude(parent_id__isnull=True).values_list('parent_id', flat=True))
        leaf_ids = list(scoped.exclude(id__in=referenced_parent_ids).values_list('id', flat=True))
        if not leaf_ids:
            break
        n, _ = Review.objects.filter(id__in=leaf_ids).delete()
        deleted_reviews += n

    _, deleted_per_model = Product.objects.filter(id__in=product_ids).delete()
    deleted_products = deleted_per_model.get('products.Product', 0)
    return deleted_products, deleted_reviews


def _prune_missing(seen_paths):
    """ ردیف‌هایی که به فایل‌های دیگر موجود در پوشه‌ی کاتالوگ اشاره می‌کنند را پاک/خالی می‌کند """
    pruned = 0

    for image in ProductImage.objects.filter(image__startswith=f'{CATALOG_DIR_NAME}/'):
        if image.image.name not in seen_paths:
            image.delete()
            pruned += 1

    for product in Product.objects.filter(main_image__startswith=f'{CATALOG_DIR_NAME}/'):
        if product.main_image.name not in seen_paths:
            product.main_image = None
            product.save(update_fields=['main_image'])
            pruned += 1

    return pruned
