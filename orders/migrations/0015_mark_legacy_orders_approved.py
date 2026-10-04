from django.db import migrations
from django.db.models import F, Q


def mark_legacy_orders_approved(apps, schema_editor):
    """
    پیش از تأیید دومرحله‌ی مدیر، فاکتور خودکار هنگام ثبت سفارش صادر می‌شد. هر سفارشی که از آن روال گذشته (فاکتور هلو
    دارد یا از مرحله‌ی «ثبت‌شده» جلوتر رفته) «تأییدشده» علامت می‌خورد تا وضعیتش برای مشتری عوض نشود و تسک بازبینی
    دوباره سراغش نرود. سفارش‌های pending بدون فاکتور دست‌نخورده می‌مانند و از روال تازه می‌گذرند.
    """
    Order = apps.get_model('orders', 'Order')
    legacy = Order.objects.filter(approved_at__isnull=True).filter(
        Q(holoo_invoice_id__isnull=False) | ~Q(status__in=['pending', 'canceled', 'rejected_stock'])
    )
    legacy.update(approved_at=F('created_at'))


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0014_order_approval_and_rejected_stock'),
    ]

    operations = [
        migrations.RunPython(mark_legacy_orders_approved, migrations.RunPython.noop),
    ]
