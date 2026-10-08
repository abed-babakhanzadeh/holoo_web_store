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


@app.on_after_finalize.connect
def setup_chat_timer_schedule(sender, **kwargs):
    # جاروی تایمرهای گفتگوی آنلاین (SLA، رفتن مشتری، بستن خودکار، غیبت کارشناس مسئول): chat/tasks.py
    sender.add_periodic_task(30.0, sender.signature('chat.tasks.sweep_chat_timers'), name='sweep-chat-timers')   # هر ۳۰ ثانیه


@app.on_after_finalize.connect
def setup_chat_retention_schedule(sender, **kwargs):
    # پاک‌سازی شبانه‌ی گفتگوهای منقضی (chat/retention.py)؛ با chat_retention_days=0 هیچ کاری نمی‌کند
    sender.add_periodic_task(crontab(hour=3, minute=40), sender.signature('chat.tasks.purge_expired_chats'),
                             name='purge-expired-chats')


@app.on_after_finalize.connect
def setup_profile_reminder_schedule(sender, **kwargs):
    # یادآوری تکمیل پروفایل؛ خودِ تسک ساعت مجاز و روشن‌بودنِ تنظیم ادمین را چک می‌کند (accounts/tasks.py)
    sender.add_periodic_task(crontab(minute=5), sender.signature('accounts.tasks.send_profile_reminders'),
                             name='send-profile-reminders')   # هر ساعت، دقیقه‌ی ۵


@app.on_after_finalize.connect
def setup_cheque_deadline_schedule(sender, **kwargs):
    # لغو خودکار سفارش چکیِ بی‌چک پس از پایان مهلت و آزادسازی موجودی (orders/tasks.py، orders/deadline.py)
    sender.add_periodic_task(15 * 60.0, sender.signature('orders.tasks.cancel_expired_cheque_orders'),
                             name='cancel-expired-cheque-orders')   # هر ۱۵ دقیقه
