"""
ترتیب پیش‌فرض «موجودها قبل از ناموجودها» برای همه‌ی فهرست‌های محصول (فروشگاه، دسته‌ها،
باکس‌های صفحه اصلی). یک نقطه‌ی مشترک تا این قاعده در هر ویو جدا تکرار نشود؛ برای افزودن
یک فهرست محصول جدید در آینده کافی‌ست به‌جای .order_by(...) از stock_first(...) استفاده شود.
"""

from django.db.models import Case, IntegerField, Value, When

from .stock import available_expression


def stock_first(queryset, *order_by):
    """
    queryset را طوری annotate/order_by می‌کند که ردیف‌های ناموجود (موجودی قابل‌فروش ≤ ۰، یعنی
    stock − رزروشده − بافر اطمینان) همیشه بعد از موجودها بیایند؛ فیلدهای order_by به‌عنوان ترتیب ثانویه داخل هر گروه (موجود/ناموجود) اعمال می‌شوند.
    """
    return queryset.annotate(_sellable=available_expression()).annotate(
        _out_of_stock=Case(When(_sellable__lt=1, then=Value(1)), default=Value(0), output_field=IntegerField())
    ).order_by('_out_of_stock', *order_by)
