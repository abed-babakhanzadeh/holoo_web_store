"""
رویدادهای دامنه‌ی حساب کاربری.

kwargs هر دو سیگنال: user
  - profile_completed : کاربر برای اولین بار پروفایلش را کامل کرد
  - profile_updated   : کاربرِ از قبل ثبت‌شده اطلاعاتش را ویرایش کرد
"""

import django.dispatch

profile_completed = django.dispatch.Signal()
profile_updated = django.dispatch.Signal()
