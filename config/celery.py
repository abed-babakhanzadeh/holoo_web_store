import os
from celery import Celery

# تنظیم ماژول پیش‌فرض تنظیمات جنگو برای سلری
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

# ساخت نمونه (Instance) از سلری با نام پروژه
app = Celery('holoo_web_store')

# خواندن تنظیمات سلری از فایل settings.py (با پیشوند CELERY_)
app.config_from_object('django.conf:settings', namespace='CELERY')

# کشف و لود کردن خودکار تسک‌ها از تمامی اپلیکیشن‌های نصب شده (مثل holoo/tasks.py)
app.autodiscover_tasks()

# زمان‌بندی تسک‌های رزرو موجودی (products/tasks.py). این‌جا و نه در config/settings.py اضافه می‌شود تا تنظیمات محلیِ
# settings.py دست‌نخورده بماند؛ با CELERY_BEAT_SCHEDULE تنظیمات ادغام می‌شود (نه جایگزین).
from celery.schedules import crontab  # noqa: E402


@app.on_after_finalize.connect
def setup_stock_reservation_schedule(sender, **kwargs):
    # add_periodic_task با beat_schedule تنظیمات ادغام می‌شود (نه جایگزین)
    sender.add_periodic_task(60.0, sender.signature('products.tasks.expire_stock_reservations'),
                             name='expire-stock-reservations')  # هر دقیقه
    sender.add_periodic_task(crontab(hour=4, minute=10), sender.signature('products.tasks.audit_stock_reservations'),
                             name='audit-stock-reservations')   # شبانه، بعد از سینک عمیق
