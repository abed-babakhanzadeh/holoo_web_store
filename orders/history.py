"""
منطق صفحه‌ی «سفارش‌های من»: جستجوی ترکیبی، چهار تب با شمارنده، فیلتر بازه/مبلغ و صفحه‌بندی.

عمداً فقط لایه‌ی کوئری است (بدون مایگریشن). نکته‌های طراحی:

* وضعیت «از دید مشتری» (Order.customer_status) به پرداخت موفق وابسته است. به‌جای یک کوئری exists به‌ازای هر سفارش
  (N+1)، کوئری اصلی `paid` را با Exists حاشیه‌نویسی می‌کند و Order.is_paid همان مقدار را می‌خواند.
* شمارنده‌ی تب‌ها عمداً با چند کوئری سبک ساخته می‌شود، نه یک aggregate شرطی: SQL Server اجازه‌ی subquery
  (Exists پرداخت) را داخل تابع تجمیعی نمی‌دهد (خطای 130).
* جستجو روی نام کالا با subquery (IN) انجام می‌شود، نه join، تا سفارش با چند ردیف مشابه تکراری برنگردد و
  distinct لازم نباشد.
"""
import re
from datetime import timedelta
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db.models import Count, Exists, OuterRef, Prefetch, Q
from django.utils import timezone

from payments.models import Transaction
from products.pricing import CHECK
from returns.models import ReturnItem, ReturnRequest
from services.text import normalize_persian, to_latin_digits

from .models import Order, OrderItem

# تسویه‌شده از دید مشتری: پرداخت موفق دارد، یا چکی است (تسویه‌ی چکی خارج از سایت است و تراکنش ندارد) - هم‌خوان با Order.customer_status
SETTLED = Q(paid=True) | Q(settlement=Order.SETTLEMENT_CHEQUE) | Q(payment_method=CHECK)

TAB_CURRENT = 'current'
TAB_DELIVERED = 'delivered'
TAB_RETURNED = 'returned'
TAB_CANCELED = 'canceled'
TABS = (
    (TAB_CURRENT, 'جاری'),
    (TAB_DELIVERED, 'تحویل شده'),
    (TAB_RETURNED, 'مرجوع شده'),
    (TAB_CANCELED, 'لغو شده'),
)
TAB_KEYS = {key for key, _ in TABS}
DEFAULT_TAB = TAB_CURRENT
PAGE_SIZE = 10
MAX_THUMBS = 6          # حداکثر تصویر بندانگشتی در نوار کارت؛ بقیه به‌صورت «+N»

DATE_RANGE_DAYS = {'7days': 7, '30days': 30, '3months': 90, 'year': 365}
AMOUNT_BOUNDS = {
    'less500': (None, 500000),
    '500-1000': (500000, 1000000),
    '1000-5000': (1000000, 5000000),
    'more5000': (5000000, None),
}
# سازگاری با لینک‌های قدیمی ?status=...
LEGACY_STATUS_TABS = {
    'delivered': TAB_DELIVERED, 'canceled': TAB_CANCELED,
    'awaiting_payment': TAB_CURRENT, 'under_review': TAB_CURRENT, 'stock_issue': TAB_CURRENT,
    'processing': TAB_CURRENT, 'shipped': TAB_CURRENT, 'awaiting_cheque': TAB_CURRENT, 'cheque_under_review': TAB_CURRENT,
    'cheque_needs_correction': TAB_CURRENT, 'cheque_approved': TAB_CURRENT,
}


def _cheque_state_annotations():
    """ وضعیت چک‌های هر سفارش برای Order.cheque_state (customer_status) بدون کوئری اضافه به‌ازای هر سفارش """
    from .models import ChequePayment

    live = ChequePayment.objects.filter(order=OuterRef('pk')).exclude(status=ChequePayment.STATUS_WITHDRAWN)
    return {
        'chq_any': Exists(live),
        'chq_pending': Exists(live.filter(status=ChequePayment.STATUS_PENDING)),
        'chq_rejected': Exists(live.filter(status=ChequePayment.STATUS_REJECTED)),
    }


def _paid_exists():
    return Exists(Transaction.objects.filter(order=OuterRef('pk'), status='success'))


def parse_search(raw):
    """ متن جستجو -> (شماره سفارش یا None، متن نرمال‌شده‌ی نام کالا)؛ ارقام فارسی/عربی پشتیبانی می‌شوند """
    text = (raw or '').strip()
    if not text:
        return None, ''
    latin = to_latin_digits(text).lstrip('#').strip()
    order_id = int(latin) if latin.isdigit() and len(latin) <= 12 else None
    return order_id, normalize_persian(text)


def _apply_common(queryset, params, *, date_field, amount_field, search_q):
    if search_q is not None:
        queryset = queryset.filter(search_q)
    days = DATE_RANGE_DAYS.get(params.get('date_range'))
    if days:
        queryset = queryset.filter(**{f'{date_field}__gte': timezone.now() - timedelta(days=days)})
    bounds = AMOUNT_BOUNDS.get(params.get('amount_range'))
    if bounds:
        low, high = bounds
        if low is not None:
            queryset = queryset.filter(**{f'{amount_field}__gte': low})
        if high is not None:
            queryset = queryset.filter(**{f'{amount_field}__lt': high})
    return queryset


def _order_search_q(order_id, name):
    if not name and order_id is None:
        return None
    condition = Q()
    if name:
        condition |= Q(pk__in=OrderItem.objects.filter(product__name_normalized__icontains=name).values('order_id'))
    if order_id is not None:
        condition |= Q(pk=order_id)
    return condition


def _return_search_q(order_id, name):
    if not name and order_id is None:
        return None
    condition = Q()
    if name:
        condition |= Q(pk__in=ReturnItem.objects.filter(
            order_item__product__name_normalized__icontains=name).values('return_request_id'))
    if order_id is not None:
        condition |= Q(order_id=order_id)
    return condition


def base_orders(user, params):
    order_id, name = parse_search(params.get('q'))
    qs = Order.objects.filter(user=user).annotate(paid=_paid_exists())
    return _apply_common(qs, params, date_field='created_at', amount_field='total_price',
                         search_q=_order_search_q(order_id, name))


def base_returns(user, params):
    order_id, name = parse_search(params.get('q'))
    qs = ReturnRequest.objects.filter(user=user)
    return _apply_common(qs, params, date_field='requested_at', amount_field='order__total_price',
                         search_q=_return_search_q(order_id, name))


def tab_orders(orders, tab):
    """ سفارش‌های یک تب (جاری/تحویل‌شده/لغوشده) از queryset پایه‌ی دارای annotate(paid) """
    if tab == TAB_CANCELED:
        return orders.filter(status='canceled')
    if tab == TAB_DELIVERED:
        return orders.filter(SETTLED, status='delivered')
    # جاری: همه‌ی سفارش‌هایی که نه لغو شده‌اند و نه (پرداخت‌شده و تحویل‌شده)؛ هم‌خوان با Order.customer_status
    return orders.exclude(status='canceled').exclude(Q(status='delivered') & SETTLED)


def tab_counts(user, params):
    orders = base_orders(user, params)
    by_status = dict(orders.order_by().values('status').annotate(n=Count('pk')).values_list('status', 'n'))
    delivered = orders.filter(SETTLED, status='delivered').count()
    canceled = by_status.get('canceled', 0)
    current = sum(n for status, n in by_status.items() if status != 'canceled') - delivered
    returned = base_returns(user, params).count()
    return {TAB_CURRENT: current, TAB_DELIVERED: delivered, TAB_RETURNED: returned, TAB_CANCELED: canceled}


def resolve_tab(params):
    tab = params.get('tab')
    if tab in TAB_KEYS:
        return tab
    return LEGACY_STATUS_TABS.get(params.get('status'), DEFAULT_TAB)


def build_query(params, **overrides):
    """ query string برای لینک تب/صفحه‌بندی؛ فقط پارامترهای معتبر و غیرخالی نگه داشته می‌شوند """
    keep = {key: params.get(key) for key in ('q', 'date_range', 'amount_range') if params.get(key)}
    keep.update({k: v for k, v in overrides.items() if v})
    return urlencode(keep)


def _decorate_orders(orders):
    for order in orders:
        items = [item for item in order.items.all() if item.product_id]
        order.thumb_items = items[:MAX_THUMBS]
        order.more_items = max(len(items) - MAX_THUMBS, 0)
        order.items_count = len(order.items.all())
    return orders


def build_history(user, params):
    """ همه‌ی داده‌ی صفحه‌ی «سفارش‌های من» برای پارامترهای GET داده‌شده """
    tab = resolve_tab(params)
    counts = tab_counts(user, params)

    if tab == TAB_RETURNED:
        queryset = base_returns(user, params).select_related('order').prefetch_related(
            Prefetch('items', queryset=ReturnItem.objects.select_related('order_item__product', 'reason')),
        ).order_by('-requested_at', '-pk')
    else:
        queryset = tab_orders(base_orders(user, params), tab).annotate(**_cheque_state_annotations()).prefetch_related(
            Prefetch('items', queryset=OrderItem.objects.select_related('product')),
        ).order_by('-created_at', '-pk')

    try:
        page_number = int(params.get('page') or 1)
    except (TypeError, ValueError):
        page_number = 1
    page_obj = Paginator(queryset, PAGE_SIZE).get_page(page_number)
    if tab != TAB_RETURNED:
        _decorate_orders(page_obj.object_list)

    tabs = [{
        'key': key, 'label': label, 'count': counts[key], 'active': key == tab,
        'url': '?' + build_query(params, tab=key if key != DEFAULT_TAB else ''),
    } for key, label in TABS]
    searching = bool((params.get('q') or '').strip())
    other_matches = sum(count for key, count in counts.items() if key != tab) if searching else 0
    return {
        'tab': tab, 'tabs': tabs, 'counts': counts, 'page_obj': page_obj,
        'is_returns_tab': tab == TAB_RETURNED,
        'orders': page_obj.object_list if tab != TAB_RETURNED else [],
        'return_requests': page_obj.object_list if tab == TAB_RETURNED else [],
        'q': (params.get('q') or '').strip(),
        'selected_date_range': params.get('date_range') or '',
        'selected_amount_range': params.get('amount_range') or '',
        'filters_active': bool(params.get('date_range') or params.get('amount_range')),
        'searching': searching, 'other_matches': other_matches,
        'page_query': build_query(params, tab=tab if tab != DEFAULT_TAB else ''),
    }
