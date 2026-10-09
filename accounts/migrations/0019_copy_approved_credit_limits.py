"""
مایگریشن داده‌ی G1: سقف تأییدشده‌ی درخواست‌های خرید چکی که پیش از این فاز تأیید شده‌اند به CustomUser.cheque_credit_limit کپی می‌شود.

  - فقط کاربری که هنوز can_purchase_with_check دارد (مجوزی که ادمین بعداً برداشته، سقف نمی‌گیرد).
  - اگر چند درخواست تأییدشده داشت، جدیدترین (decided_at، بعد id) ملاک است.
  - درخواستِ تأییدشده‌ی بدون approved_limit (در F3 اختیاری بود) و کاربرانِ بدون درخواست (مثل مشتریان چکیِ سطح ۱) NULL = بدون سقف می‌مانند؛
    یعنی هیچ کاربرِ موجودی با این مایگریشن ناگهان محدود نمی‌شود.
  - برگشت‌پذیر: reverse فقط ستون را دست‌نخورده می‌گذارد (حذف ستون در مایگریشن قبلی انجام می‌شود).
"""
from django.db import migrations


def copy_limits(apps, schema_editor):
    Request = apps.get_model('accounts', 'ChequeCreditRequest')
    User = apps.get_model('accounts', 'CustomUser')
    seen = set()
    approved = (Request.objects.filter(status='approved', approved_limit__isnull=False)
                .order_by('-decided_at', '-id').values_list('user_id', 'approved_limit'))
    for user_id, limit in approved:
        if user_id in seen:
            continue
        seen.add(user_id)
        User.objects.filter(pk=user_id, can_purchase_with_check=True).update(cheque_credit_limit=limit)


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0018_cheque_credit_limit'),
    ]

    operations = [
        migrations.RunPython(copy_limits, migrations.RunPython.noop),
    ]
