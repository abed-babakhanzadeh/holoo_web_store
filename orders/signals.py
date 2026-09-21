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
