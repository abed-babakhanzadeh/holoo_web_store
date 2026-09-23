"""
Backfill تأیید تجاری برای کاربران موجود (پیش از معرفی approval_status در 0011).

معیار سه‌گانه (تأییدشده): پروفایل کامل + status=ACTIVE + حداقل یک سفارش غیرلغوشده با حداقل
یک تراکنش status='success'. فقط approval_status/approved_at نوشته می‌شود؛ price_level و بقیه‌ی
فیلدها دست‌نخورده می‌مانند. کاربرِ از‌قبل REJECTED هرگز لمس نمی‌شود (فیلتر روی approval_status=
'PENDING' است). Idempotent: اجرای دوباره صفر ردیف تغییر می‌دهد (فقط PENDING را هدف می‌گیرد،
و بعد از اجرای اول دیگر هیچ کاندیدی PENDING نیست).
"""

from django.db import migrations
from django.utils import timezone


def backfill_approved_customers(apps, schema_editor):
    # فقط مدل‌های تاریخی (frozen state همین migration)؛ عمداً از accounts.models/orders.models/
    # payments.models چیزی import نشده — مدل‌های لایو ممکن است فیلد/متدی داشته باشند که در این
    # نقطه از تاریخ دیتابیس هنوز وجود نداشت
    CustomUser = apps.get_model('accounts', 'CustomUser')
    Order = apps.get_model('orders', 'Order')
    Transaction = apps.get_model('payments', 'Transaction')

    candidates = CustomUser.objects.filter(approval_status='PENDING', status='ACTIVE') \
        .exclude(first_name='').exclude(first_name__isnull=True) \
        .exclude(last_name='').exclude(last_name__isnull=True) \
        .exclude(national_code='').exclude(national_code__isnull=True)

    # «زمان گذار به ساختار جدید تأیید تجاری» (Migration Transition Date) — نه تاریخ واقعی
    # تأییدشدنِ این مشتری توسط مدیر (که هیچ‌جا ثبت نشده بود)، صرفاً لحظه‌ی اجرای همین migration
    transition_time = timezone.now()

    approved_ids = []
    for user in candidates:
        has_valid_paid_order = Order.objects.filter(user_id=user.pk).exclude(status='canceled').filter(
            id__in=Transaction.objects.filter(status='success').values('order_id')
        ).exists()
        if has_valid_paid_order:
            approved_ids.append(user.pk)

    if approved_ids:
        # .update() عمداً به‌جای save() تک‌تک: فقط دو ستون نوشته می‌شود، هیچ فیلد دیگری
        # (از جمله price_level) لمس نمی‌شود
        CustomUser.objects.filter(pk__in=approved_ids).update(
            approval_status='APPROVED', approved_at=transition_time,
        )


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0011_customuser_approval_status_customuser_approved_at_and_more'),
        ('orders', '0011_order_coupon_snapshot'),
        ('payments', '0001_initial'),
    ]

    operations = [
        # عمداً بدون reverse واقعی: برگرداندن این کاربران به PENDING یک تصمیم کسب‌وکاری است،
        # نه یک عملیات فنی خودکار (همان الگوی products/migrations/0022_sitesettings_guest_pricing.py)
        migrations.RunPython(backfill_approved_customers, migrations.RunPython.noop),
    ]
