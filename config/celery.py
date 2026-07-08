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