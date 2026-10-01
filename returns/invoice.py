"""
داده‌ی صورت‌حساب برگشت از فروش (templates/returns/invoice.html). فقط پس از قطعی‌شدن مبلغ‌ها (REFUND_PENDING به بعد)
صادر می‌شود؛ مبلغ هر قلم همان refund_amount ثبت‌شده در بازرسی است، نه محاسبه‌ی دوباره.
"""
from decimal import Decimal

from .models import ReturnRequest

INVOICE_STATUSES = (ReturnRequest.STATUS_REFUND_PENDING, ReturnRequest.STATUS_COMPLETED)


def can_issue_return_invoice(return_request):
    return return_request.status in INVOICE_STATUSES


def build_return_invoice(return_request, items):
    """ items: ReturnItem ها با order_item→product/color و reason بارگذاری‌شده """
    rows = []
    for index, ri in enumerate(items, start=1):
        order_item = ri.order_item
        product = order_item.product
        quantity = ri.approved_quantity if ri.approved_quantity is not None else ri.requested_quantity
        rows.append({
            'n': index,
            'code': (product.product_code or '') if product else '',
            'system_id': product.erp_code if product else '',
            'name': product.name if product else 'کالای حذف‌شده',
            'color': order_item.color.name if order_item.color else '',
            'unit': (product.unit if product else '') or '',
            'quantity': quantity,
            'unit_price': order_item.price,
            'reason': ri.reason.title,
            'amount': ri.refund_amount or Decimal('0'),
        })
    zero = Decimal('0')
    return {
        'rows': rows,
        'sum_quantity': sum((r['quantity'] for r in rows), 0),
        'sum_amount': sum((r['amount'] for r in rows), zero),
        'shipping_refund': return_request.shipping_refund_amount if return_request.shipping_refunded else zero,
        'total': return_request.total_refund_amount,
        'date': return_request.completed_at or return_request.item_received_at or return_request.requested_at,
    }
