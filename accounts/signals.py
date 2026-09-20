"""
رویدادهای دامنه‌ی حساب کاربری.

kwargs هر دو سیگنال: user
  - profile_completed : کاربر برای اولین بار پروفایلش را کامل کرد
  - profile_updated   : کاربرِ از قبل ثبت‌شده اطلاعاتش را ویرایش کرد

default_address_changed (kwargs: user, address): آدرس پیش‌فرض کاربر عوض شد یا خودِ آدرس پیش‌فرض
  ویرایش شد (پس از commit تراکنش صدا زده می‌شود). شنونده‌ها مثل holoo با همین رویداد همگام می‌مانند.
"""

import django.dispatch

profile_completed = django.dispatch.Signal()
profile_updated = django.dispatch.Signal()
default_address_changed = django.dispatch.Signal()
