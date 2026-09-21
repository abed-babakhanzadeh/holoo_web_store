"""
فیلتر «فقط کالاهای دارای تخفیف» و مرتب‌سازی «بیشترین تخفیف» برای لیست محصولات فروشگاه.

اصل: هیچ منطق قیمتی دوم ساخته نمی‌شود. «کالای دارای تخفیف» یعنی همان کالایی که موتور قیمت (products.pricing.price_breakdown،
همان چیزی که روی کارت، سبد و فاکتور می‌نشیند) برای *همین کاربر و روش پرداخت* واقعاً تخفیف می‌دهد؛ پس کاربر ویژه با
apply_to_vip خاموش، یا تخفیفِ ویژه‌ی یک سطح قیمت، فیلتر و ترتیب درست خودش را می‌بیند.

هزینه (بدون کوئری موازی):
  ۱. شاخص تخفیف‌ها از کش می‌آید (promotions.index؛ هیچ کوئری‌ای)؛ فقط قواعدِ در بازه و مخاطب‌خورده‌ی این کاربر نگه داشته می‌شوند.
  ۲. *یک* کوئری روی کوئری‌ستِ فیلترشده‌ی خودِ ویو (جستجو/دسته/برند/... همه رعایت می‌شوند) فقط برای کالاهای «کاندید»
     (اجتماعِ اهداف همان قواعد) و فقط با ستون‌های لازم برای قیمت (.only)؛ نه کل کاتالوگ.
  ۳. محاسبه‌ی قیمتِ هر کاندید درون‌حافظه‌ای با همان موتور (بدون کوئری).
  مرتب‌سازی «بیشترین تخفیف» علاوه بر این یک کوئریِ سبک از (id, stock, created_at) کوئری‌ستِ فیلترشده می‌زند و ترتیب را در
  پایتون می‌سازد؛ صفحه‌بندی روی فهرست شناسه‌هاست و فقط ۱۲ کالای همان صفحه بار می‌شود.

ترتیب «بیشترین تخفیف»: کالای موجود قبل از ناموجود (قاعده‌ی سراسری سایت)، سپس درصد تخفیف نزولی، سپس مبلغ تخفیف، سپس جدیدترین.
کالاهای بدون تخفیف بعد از کالاهای تخفیف‌دار (با همان قاعده‌ی موجود/ناموجود) و به‌ترتیب جدیدترین می‌آیند.
"""

from dataclasses import dataclass, field

from django.db.models import Q
from django.utils import timezone

from products.pricing import default_payment_method, price_breakdown

from .flash import rule_product_filter
from .index import get_index
from .resolver import _audience_ok, _loyalty_index, _policy_allows

# بیشترین تعداد شناسه‌ای که با pk__in به کوئری اصلی داده می‌شود (SQL Server ≈ ۲۱۰۰ پارامتر)؛ بیشتر از آن به Qِ قواعد برمی‌گردیم
MAX_PK_IN = 1500

# فقط ستون‌هایی که قیمت‌گذاری/مرتب‌سازی می‌خواند
PRICING_FIELDS = ('category', 'brand', 'stock', 'created_at', 'price') + tuple(f'price{i}' for i in range(2, 11))


@dataclass(frozen=True)
class DiscountCatalog:
    """ نتیجه‌ی محاسبه برای یک کاربر و یک کوئری‌ست: شناسه ← (درصد، مبلغ تخفیف هر واحد) """
    percents: dict = field(default_factory=dict)
    candidate_q: object = field(default=None, compare=False)

    @property
    def count(self):
        return len(self.percents)

    def filter_queryset(self, queryset):
        """ کوئری‌ست را به کالاهای دارای تخفیف محدود می‌کند (دقیق؛ فقط برای فهرست‌های خیلی بزرگ به اجتماع اهداف قواعد برمی‌گردد) """
        if not self.percents:
            return queryset.none()
        if self.count <= MAX_PK_IN:
            return queryset.filter(pk__in=list(self.percents))
        return queryset.filter(self.candidate_q)

    def ordered_ids(self, queryset):
        """ شناسه‌ی همه‌ی کالاهای کوئری‌ست به ترتیب «بیشترین تخفیف» (نگاه کنید سرِ ماژول) """
        rows = list(queryset.select_related(None).prefetch_related(None).order_by().values_list('id', 'stock', 'created_at'))
        percents = self.percents

        def key(row):
            pk, stock, created_at = row
            percent, amount = percents.get(pk, (0, 0))
            return (1 if (stock or 0) <= 0 else 0, -percent, -amount, -created_at.timestamp() if created_at else 0, pk)

        return [row[0] for row in sorted(rows, key=key)]


EMPTY = DiscountCatalog()


def build_discount_catalog(user, queryset, now=None):
    """ کالاهای دارای تخفیفِ واقعی برای این کاربر در کوئری‌ست داده‌شده (یک کوئری؛ نگاه کنید سرِ ماژول) """
    now = now or timezone.now()
    index = get_index()
    method = default_payment_method(user)
    if not _policy_allows(index.policy, user, method):
        return EMPTY

    loyalty = []

    def loyalty_level():
        if not loyalty:
            loyalty.append(_loyalty_index(user))
        return loyalty[0]

    rules = [r for r in index.rules if r.in_window(now) and _audience_ok(r, user, method, loyalty_level)]
    if not rules:
        return EMPTY

    candidate_q = Q()
    for rule in rules:
        candidate_q |= rule_product_filter(rule)

    candidates = (queryset.select_related(None).prefetch_related(None).order_by().filter(candidate_q).only(*PRICING_FIELDS))
    percents = {}
    for product in candidates:
        breakdown = price_breakdown(product, user, method, now=now)
        if breakdown.has_discount:
            percents[product.pk] = (breakdown.percent, int(breakdown.discount_amount))
    return DiscountCatalog(percents=percents, candidate_q=candidate_q)
