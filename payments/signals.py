"""
رویدادهای دامنه‌ی پرداخت.

اپ payments فقط اعلام می‌کند «پرداختی موفق شد»؛ اینکه در پی آن سند حسابداری ثبت شود یا
پیامک برود یا هر دو، وظیفه‌ی شنونده‌هاست نه خودِ پرداخت.
"""

import django.dispatch

# kwargs: order, transaction
payment_succeeded = django.dispatch.Signal()
