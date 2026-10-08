"""
رویدادهای دامنه‌ی سفارش.

اپ orders فقط اعلام می‌کند «سفارشی ثبت شد» و هیچ نمی‌داند چه کسی به آن گوش می‌دهد.
قبلاً مستقیم `from holoo.tasks import send_order_to_holoo` می‌کرد؛ یعنی برای تعویض نرم‌افزار
حسابداری باید خودِ اپ سفارش دست می‌خورد.
"""

import django.dispatch

# kwargs: order
order_placed = django.dispatch.Signal()

# kwargs: order — وقتی وضعیت سفارش به «لغو شده» تغییر کند (فقط از مسیر Order.save؛ مثلاً ادمین). شنونده‌ها منابع
# رزروشده‌ی سفارش (مثل ظرفیت کد تخفیف) را آزاد می‌کنند.
order_canceled = django.dispatch.Signal()

# kwargs: order — مدیر سفارش را «تأیید» کرد (orders/approval.py). فقط از این پس فاکتور قطعی در حسابداری ثبت می‌شود؛
# ثبت سفارش و پرداخت به‌تنهایی انبار/حسابداری را تغییر نمی‌دهد.
order_approved = django.dispatch.Signal()

# --- چرخه‌ی چک (فاز E): اپ orders فقط اعلام می‌کند؛ اعلان‌ها در notifications/receivers.py وصل می‌شوند ---
# kwargs: order, cheque, resubmitted — چک تازه ثبت شد یا چکِ ردشده اصلاح و دوباره برای بررسی فرستاده شد (resubmitted=True)
cheque_submitted = django.dispatch.Signal()

# kwargs: order, cheque, status ('approved'|'rejected'), reason — مدیر چک را تأیید/رد کرد
cheque_reviewed = django.dispatch.Signal()

# kwargs: order — سفارش به‌دلیل پایان مهلت ثبت/اصلاح چک خودکار لغو شد (orders/deadline.py)
cheque_deadline_expired = django.dispatch.Signal()
