"""
رویدادهای دامنه‌ی مرجوعی کالا.

اپ returns هرگز مستقیم notify()/notify_admin() را صدا نمی‌زند - فقط این سیگنال‌ها را شلیک
می‌کند؛ اینکه در پی هرکدام پیامکی برود یا نه، وظیفه‌ی notifications/receivers.py است (همان
معماری payments.signals.payment_succeeded / orders.signals.order_canceled).
"""

import django.dispatch

# kwargs: return_request
return_requested = django.dispatch.Signal()
# kwargs: return_request
return_approved = django.dispatch.Signal()
# kwargs: return_request
return_item_received = django.dispatch.Signal()
# kwargs: return_request, reason
return_rejected = django.dispatch.Signal()
# kwargs: return_request
return_refund_completed = django.dispatch.Signal()
