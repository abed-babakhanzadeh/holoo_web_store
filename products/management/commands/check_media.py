from django.apps import apps
from django.core.management.base import BaseCommand
from django.db.models import FileField


class Command(BaseCommand):
    help = (
        'فقط‌خواندنی: همه‌ی فیلدهای تصویر/فایل در دیتابیس را می‌گردد و می‌گوید کدام فایل‌ها روی دیسک (media/) نیستند '
        '(یعنی در سایت «عکس شکسته» می‌شوند ولی متنشان نمایش داده می‌شود). چیزی نمی‌نویسد، حذف یا لمس نمی‌کند.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=15, help='حداکثر تعداد نمونه‌ی فایل گمشده برای هر فیلد')

    def handle(self, *args, **opts):
        total_missing = total_checked = 0
        for model in apps.get_models():
            for field in model._meta.get_fields():
                if not isinstance(field, FileField) or field.model is not model or model._meta.abstract:
                    continue
                names = list(model._default_manager.exclude(**{field.name: ''}).exclude(**{f'{field.name}__isnull': True})
                             .values_list('pk', field.name))
                if not names:
                    continue
                missing = [(pk, name) for pk, name in names if not field.storage.exists(name)]
                total_checked += len(names)
                total_missing += len(missing)
                label = f'{model._meta.label}.{field.name}'
                if missing:
                    self.stdout.write(self.style.WARNING(f'{label}: {len(missing)} از {len(names)} فایل گم شده'))
                    for pk, name in missing[:opts['limit']]:
                        self.stdout.write(f'    #{pk}  {name}')
                else:
                    self.stdout.write(f'{label}: همه {len(names)} فایل موجودند')
        style = self.style.ERROR if total_missing else self.style.SUCCESS
        self.stdout.write(style(f'\nجمع: {total_missing} فایل گم‌شده از {total_checked} فایلِ ثبت‌شده در دیتابیس.'))
