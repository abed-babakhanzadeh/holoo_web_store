"""آمار تراکنش‌های کاربر برای پیشخوان پنل کاربری."""

from accounts.stats import register

from .models import Transaction


@register('transactions_recent')
def transactions_recent(user):
    return list(
        Transaction.objects.filter(user=user, status='success')
        .select_related('order').order_by('-updated_at')[:5]
    )
