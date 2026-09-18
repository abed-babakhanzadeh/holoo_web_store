"""
ترتیب پیش‌فرض «موجودها قبل از ناموجودها» برای همه‌ی فهرست‌های محصول (فروشگاه، دسته‌ها،
باکس‌های صفحه اصلی). یک نقطه‌ی مشترک تا این قاعده در هر ویو جدا تکرار نشود؛ برای افزودن
یک فهرست محصول جدید در آینده کافی‌ست به‌جای .order_by(...) از stock_first(...) استفاده شود.
"""

from django.db.models import Case, IntegerField, Value, When


def stock_first(queryset, *order_by):
    """
    queryset را طوری annotate/order_by می‌کند که ردیف‌های stock<=0 همیشه بعد از stock>0
    بیایند؛ فیلدهای order_by به‌عنوان ترتیب ثانویه داخل هر گروه (موجود/ناموجود) اعمال می‌شوند.
    """
    return queryset.annotate(
        _out_of_stock=Case(When(stock__lte=0, then=Value(1)), default=Value(0), output_field=IntegerField())
    ).order_by('_out_of_stock', *order_by)
