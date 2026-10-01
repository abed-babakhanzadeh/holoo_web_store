"""
تأمین‌کننده‌ی «خریدار مرجوع‌کرده» برای نمایش نظرات (reviews.purchases.register_returned_provider).

نظر کسی که کالا را مرجوع کرده درباره‌ی کیفیت و تجربه‌ی واقعی کالا ارزشمند است؛ پس نشان «خریدار» برداشته نمی‌شود و فقط
با برچسب «مرجوع شده» نمایش داده می‌شود. «مرجوع شده» یعنی کالا واقعاً به فروشگاه برگشته: وضعیت‌های «کالا دریافت شد»،
«در صف بازپرداخت» و «تکمیل»، و (اگر بازرسی تعداد تأییدشده را ثبت کرده) حداقل یک واحد تأییدشده. درخواستِ هنوز در
انتظار/تأییدشده‌ی بدون دریافت کالا و درخواست ردشده مرجوع حساب نمی‌شود.
"""
from django.db.models import Q

from reviews.purchases import register_returned_provider

from .models import ReturnItem, ReturnRequest

RETURNED_STATUSES = (
    ReturnRequest.STATUS_ITEM_RECEIVED, ReturnRequest.STATUS_REFUND_PENDING, ReturnRequest.STATUS_COMPLETED,
)


@register_returned_provider
def returned_user_ids(product, user_ids):
    return set(
        ReturnItem.objects
        .filter(order_item__product=product, return_request__user_id__in=user_ids,
                return_request__status__in=RETURNED_STATUSES)
        .filter(Q(approved_quantity__isnull=True) | Q(approved_quantity__gt=0))
        .values_list('return_request__user_id', flat=True)
    )
