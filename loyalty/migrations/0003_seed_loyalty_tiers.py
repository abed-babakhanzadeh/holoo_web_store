"""
Seed ایدمپوتنت ۵ سطح اولیه‌ی باشگاه مشتریان (Phase 3B - جدول مصوب).

مقادیر عمداً از SiteSettings کپی نشده‌اند (تصمیم صریح فاز ۳B: سطوح جدید مستقل و بر مبنای
lifetime_earned لجر طراحی شده‌اند، نه بازتاب آستانه‌های قدیمیِ سیستم زنده‌ی CustomUser).
"""

from django.db import migrations

SEED_TIERS = [
    {'rank': 0, 'title': 'مشتری پایه', 'threshold': 0, 'badge_color': '#9CA3AF'},
    {'rank': 1, 'title': 'برنزی', 'threshold': 200, 'badge_color': '#CD7F32'},
    {'rank': 2, 'title': 'نقره‌ای', 'threshold': 500, 'badge_color': '#C0C0C0'},
    {'rank': 3, 'title': 'طلایی', 'threshold': 1200, 'badge_color': '#FFD700'},
    {'rank': 4, 'title': 'الماسی', 'threshold': 2500, 'badge_color': '#38BDF8'},
]


def seed_loyalty_tiers(apps, schema_editor):
    """
    ثبت ایدمپوتنت: get_or_create بر مبنای rank. اگر ردیفی با همان rank از قبل وجود دارد (چه از
    اجرای قبلی همین مایگریشن، چه چون ادمین دستی سطحی با همان rank ساخته)، هیچ فیلدی از آن
    بازنویسی نمی‌شود - فقط اگر واقعاً غایب باشد ساخته می‌شود. اجرای دوباره‌ی این مایگریشن هرگز
    IntegrityError نمی‌دهد و هرگز تغییرات دستی بعدی ادمین را پاک نمی‌کند.
    """
    LoyaltyTier = apps.get_model('loyalty', 'LoyaltyTier')
    for data in SEED_TIERS:
        LoyaltyTier.objects.get_or_create(
            rank=data['rank'],
            defaults={
                'title': data['title'],
                'threshold': data['threshold'],
                'badge_color': data['badge_color'],
                'is_active': True,
            },
        )


def unseed_loyalty_tiers(apps, schema_editor):
    """
    معکوسِ تمیز: فقط ردیف‌هایی حذف می‌شوند که هنوز *دقیقاً* همان مقادیر اولیه‌ی همین مایگریشن را
    دارند. اگر ادمین یکی از این ۵ سطح را از آن زمان دستی ویرایش کرده باشد، دیگر با این فیلتر
    مطابقت ندارد و در رول‌بک دست‌نخورده می‌ماند - reverse نباید تغییرات دستی بعدی را نابود کند.
    """
    LoyaltyTier = apps.get_model('loyalty', 'LoyaltyTier')
    for data in SEED_TIERS:
        LoyaltyTier.objects.filter(
            rank=data['rank'], title=data['title'],
            threshold=data['threshold'], badge_color=data['badge_color'],
        ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('loyalty', '0002_loyaltytier'),
    ]

    operations = [
        migrations.RunPython(seed_loyalty_tiers, unseed_loyalty_tiers),
    ]
