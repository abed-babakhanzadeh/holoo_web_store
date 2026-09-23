"""
رویدادهای دامنه‌ی حساب کاربری.

kwargs هر دو سیگنال: user
  - profile_completed : کاربر برای اولین بار پروفایلش را کامل کرد
  - profile_updated   : کاربرِ از قبل ثبت‌شده اطلاعاتش را ویرایش کرد

default_address_changed (kwargs: user, address): آدرس پیش‌فرض کاربر عوض شد یا خودِ آدرس پیش‌فرض
  ویرایش شد (پس از commit تراکنش صدا زده می‌شود). شنونده‌ها مثل holoo با همین رویداد همگام می‌مانند.

سیگنال‌های چرخه‌ی تأیید تجاری (accounts.models.CustomUser.approve/reject/resubmit_for_review/
revoke_approval_due_to_identity_change) — همه‌شان kwargs: user، و طبق همان الگوی
default_address_changed، همیشه از داخل transaction.on_commit (پس از commit، بعد از آزاد شدن
قفل ردیفی کاربر) شلیک می‌شوند، نه بلافاصله داخل بلوک اتمیک:
  - user_approved                       : مدیر کاربر را تأیید و سطح قیمتش را تعیین کرد
  - user_rejected                       : مدیر کاربر را رد کرد
  - user_resubmitted_for_review         : کاربرِ ردشده با اکشن صریح دوباره درخواست بررسی داد
  - user_identity_changed_after_approval: کاربرِ از‌قبل‌تأییدشده نام/نام‌خانوادگی/کد ملی‌اش را
    تغییر داد و تأییدش خودکار لغو شد (approval_status → PENDING)
"""

import django.dispatch

profile_completed = django.dispatch.Signal()
profile_updated = django.dispatch.Signal()
default_address_changed = django.dispatch.Signal()

user_approved = django.dispatch.Signal()
user_rejected = django.dispatch.Signal()
user_resubmitted_for_review = django.dispatch.Signal()
user_identity_changed_after_approval = django.dispatch.Signal()
