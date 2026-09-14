from django.db import migrations, models


def installment_to_vip(apps, schema_editor):
    Order = apps.get_model('orders', 'Order')
    Order.objects.filter(payment_method='installment').update(payment_method='vip')


def vip_to_installment(apps, schema_editor):
    Order = apps.get_model('orders', 'Order')
    Order.objects.filter(payment_method='vip').update(payment_method='installment')


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0004_orderitem_color'),
    ]

    operations = [
        migrations.RunPython(installment_to_vip, vip_to_installment),
        migrations.AlterField(
            model_name='order',
            name='payment_method',
            field=models.CharField(
                choices=[
                    ('check', 'چکی (قیمت 1)'),
                    ('cash', 'نقدی (قیمت 2)'),
                    ('vip', 'ویژه (قیمت 3)'),
                ],
                default='cash',
                max_length=20,
                verbose_name='روش پرداخت',
            ),
        ),
    ]
