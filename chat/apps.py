from django.apps import AppConfig
from django.db.models.signals import post_migrate

OPERATOR_GROUP_NAME = 'کارشناس پشتیبانی'
OPERATOR_PERMISSION = 'operate_chat'


def ensure_operator_group(sender, **kwargs):
    """
    گروه «کارشناس پشتیبانی» با مجوز chat.operate_chat را (بدون دست‌زدن به گروه‌ها/مجوزهای دیگر و بدون بازنویسی عضویت‌ها) بعد از هر
    migrate می‌سازد. فقط همین گروه (و superuser) به پیشخوان گفتگو دسترسی دارد، نه هر staff.
    """
    from django.contrib.auth.models import Group, Permission

    try:
        permission = Permission.objects.get(content_type__app_label='chat', codename=OPERATOR_PERMISSION)
    except Permission.DoesNotExist:
        return                                      # مجوزها هنوز ساخته نشده‌اند (migrate جزئی)؛ بار بعد
    group, _ = Group.objects.get_or_create(name=OPERATOR_GROUP_NAME)
    group.permissions.add(permission)


class ChatConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'chat'
    verbose_name = 'گفتگوی آنلاین'

    def ready(self):
        from . import signals  # noqa: F401 - اتصال گفتگوهای مهمان به حساب بعد از ورود

        post_migrate.connect(ensure_operator_group, sender=self, dispatch_uid='chat_ensure_operator_group')
