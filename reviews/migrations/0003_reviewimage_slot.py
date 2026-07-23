from collections import defaultdict

from django.db import migrations, models


def backfill_slots(apps, schema_editor):
    """ به تصاویر موجود هر نظر، بر اساس ترتیب ثبت (id)، شماره‌های ۱ تا ۳ می‌دهد """
    ReviewImage = apps.get_model('reviews', 'ReviewImage')
    counters = defaultdict(int)
    for image in ReviewImage.objects.order_by('review_id', 'id'):
        counters[image.review_id] += 1
        image.slot = counters[image.review_id]
        image.save(update_fields=['slot'])


class Migration(migrations.Migration):

    dependencies = [
        ('reviews', '0002_review_color'),
    ]

    operations = [
        migrations.AddField(
            model_name='reviewimage',
            name='slot',
            field=models.PositiveSmallIntegerField(default=1, verbose_name='شماره تصویر'),
            preserve_default=False,
        ),
        migrations.RunPython(backfill_slots, reverse_code=migrations.RunPython.noop),
        migrations.AlterModelOptions(
            name='reviewimage',
            options={'ordering': ('slot',), 'verbose_name': 'تصویر نظر', 'verbose_name_plural': 'تصاویر نظرات'},
        ),
        migrations.AlterUniqueTogether(
            name='reviewimage',
            unique_together={('review', 'slot')},
        ),
    ]
