"""
مسیر کامل «اطلاع بده وقتی موجود شد» از سینک هلو تا پیامک (حسابرسی ۱۷ اکتبر ۲۰۲۶): StockAlert ← سینک ← سیگنال ← Notification ←
(تسک ارسال). تا اینجا هر حلقه جدا تست شده بود؛ این فایل کل زنجیره را یک‌جا می‌سنجد. به هلو/پیامک واقعی نمی‌رود.
"""
from unittest import mock

from django.core.cache import cache
from django.test import TestCase

from accounts.testing import make_approved_user
from holoo.tasks import sync_products_from_holoo
from notifications.models import Notification
from products.models import Category, Product, StockAlert


def holoo_item(erp, few, spd=None, **extra):
    item = {'ErpCode': erp, 'Name': 'کالای تست موجودی', 'Code': '777', 'Few': few, 'SellPrice': 100000, 'IsActive': True,
            **{f'SellPrice{i}': 0 for i in range(2, 11)}}
    if spd is not None:
        item['FewSpd'] = spd
    item.update(extra)
    return item


class BackInStockEndToEndTests(TestCase):
    def setUp(self):
        cache.delete('lock:holoo:product_sync')
        category = Category.objects.create(name='تست', slug='bis-flow-cat')
        self.product = Product.objects.create(name='کالای تست موجودی', slug='bis-flow-p', erp_code='ERP-BIS-1', category=category,
                                              price=100000, stock=0)
        self.user = make_approved_user('09125550301')

    def alert(self, channel=StockAlert.CHANNEL_SMS):
        return StockAlert.objects.create(product=self.product, user=self.user, channel=channel)

    def sync(self, item):
        with mock.patch('holoo.client.HolooClient.get_product_count', return_value=1), \
             mock.patch('holoo.client.HolooClient.get_products', return_value={'product': [item]}), \
             mock.patch('notifications.tasks.deliver_notification.delay'), self.captureOnCommitCallbacks(execute=True):
            return sync_products_from_holoo()

    def sms(self):
        return Notification.objects.filter(template_key='back_in_stock_sms', recipient=self.user.phone_number)

    def test_sms_alert_is_sent_when_the_sync_sees_stock_go_from_zero_to_ten(self):
        alert = self.alert()
        self.sync(holoo_item('ERP-BIS-1', few=10))
        self.assertEqual(self.sms().count(), 1)
        self.assertIn('دوباره موجود شد', self.sms().get().text)
        alert.refresh_from_db()
        self.assertEqual(alert.status, StockAlert.STATUS_NOTIFIED)

    def test_no_sms_while_the_sync_still_sees_zero(self):
        self.alert()
        self.sync(holoo_item('ERP-BIS-1', few=0))
        self.assertEqual(self.sms().count(), 0)

    def test_a_second_sync_does_not_resend(self):
        self.alert()
        self.sync(holoo_item('ERP-BIS-1', few=10))
        self.sync(holoo_item('ERP-BIS-1', few=10))
        self.assertEqual(self.sms().count(), 1)

    def test_manual_stock_that_leaves_the_sellable_column_at_zero_does_not_notify(self):
        """
        هلو دو ستون دارد: Few (موجودی) و FewSpd (موجودی منهای سفارش/پیش‌فاکتور باز). سایت min(Few، FewSpd) را می‌فروشد چون هلو
        فاکتورِ بیشتر از FewSpd را با خطای ۲۸ رد می‌کند. اگر موجودی دستی فقط Few را عوض کند و FewSpd صفر بماند، کالا واقعاً
        قابل‌فروش نیست و پیامک «موجود شد» نباید برود (وگرنه مشتری می‌آید و سفارشش رد می‌شود).
        """
        self.alert()
        self.sync(holoo_item('ERP-BIS-1', few=10, spd=0))
        self.assertEqual(self.sms().count(), 0)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 0)

    def test_pending_alert_survives_until_the_product_is_really_sellable(self):
        alert = self.alert()
        self.sync(holoo_item('ERP-BIS-1', few=10, spd=0))
        alert.refresh_from_db()
        self.assertEqual(alert.status, StockAlert.STATUS_PENDING)
        self.sync(holoo_item('ERP-BIS-1', few=10, spd=10))
        self.assertEqual(self.sms().count(), 1)


class SweepStockAlertsTests(TestCase):
    """
    holoo/stock_alerts.py::sweep_stock_alerts (تسک sweep_stock_alerts، هر ۱۰ دقیقه): بر پایه‌ی وضعیتِ دیتابیس سایت، بدون هیچ تماس با هلو.
    تصمیم کارفرما: تأخیر مهم نیست، رسیدن پیام مهم است و به‌ازای درخواست‌ها نباید هلو صدا زده شود.
    """

    def setUp(self):
        category = Category.objects.create(name='تست', slug='bis-sweep-cat')
        self.product = Product.objects.create(name='کالای تست موجودی', slug='bis-sweep-p', erp_code='ERP-BIS-2', product_code='00777',
                                              category=category, price=100000, stock=0)
        self.user = make_approved_user('09125550302')
        self.alert = StockAlert.objects.create(product=self.product, user=self.user, channel=StockAlert.CHANNEL_SMS)

    def sweep(self):
        from holoo.stock_alerts import sweep_stock_alerts
        with mock.patch('holoo.client.requests') as http, mock.patch('notifications.tasks.deliver_notification.delay'), \
             self.captureOnCommitCallbacks(execute=True):
            result = sweep_stock_alerts()
        self.assertFalse(http.get.called or http.post.called)             # هیچ تماسی به هلو نیست
        return result

    def sms(self):
        return Notification.objects.filter(template_key='back_in_stock_sms', recipient=self.user.phone_number)

    def test_unavailable_product_keeps_its_alert_pending(self):
        self.assertEqual(self.sweep(), 'products_with_pending_alerts=1 notified_products=0')
        self.assertEqual(self.sms().count(), 0)
        self.alert.refresh_from_db()
        self.assertEqual(self.alert.status, StockAlert.STATUS_PENDING)

    def test_a_missed_signal_is_recovered_by_the_state_based_sweep(self):
        """ موجودی بالا رفته ولی سیگنالِ سینک هرگز نیامد (قطعی/ری‌استارت): جاروب بر پایه‌ی وضعیت پیام را می‌فرستد """
        Product.objects.filter(pk=self.product.pk).update(stock=10)
        self.sweep()
        self.assertEqual(self.sms().count(), 1)
        self.alert.refresh_from_db()
        self.assertEqual(self.alert.status, StockAlert.STATUS_NOTIFIED)

    def test_notified_alerts_are_never_resent(self):
        Product.objects.filter(pk=self.product.pk).update(stock=10)
        self.sweep()
        self.sweep()
        self.assertEqual(self.sms().count(), 1)

    def test_reservation_released_by_a_cancelled_order_notifies_too(self):
        Product.objects.filter(pk=self.product.pk).update(stock=5, reserved_quantity=5)
        self.sweep()
        self.assertEqual(self.sms().count(), 0)                            # هنوز رزروشده
        Product.objects.filter(pk=self.product.pk).update(reserved_quantity=0)
        self.sweep()
        self.assertEqual(self.sms().count(), 1)

    def test_the_safety_buffer_is_respected(self):
        from products.models import SiteSettings
        SiteSettings.objects.update_or_create(pk=1, defaults={'stock_safety_buffer': 2})
        cache.delete(SiteSettings.CACHE_KEY)
        self.addCleanup(cache.delete, SiteSettings.CACHE_KEY)
        Product.objects.filter(pk=self.product.pk).update(stock=2)
        self.sweep()
        self.assertEqual(self.sms().count(), 0)
        Product.objects.filter(pk=self.product.pk).update(stock=3)
        self.sweep()
        self.assertEqual(self.sms().count(), 1)

    def test_many_pending_alerts_on_one_product_still_cost_one_pass(self):
        users = [make_approved_user(f'0912555{4000 + i}') for i in range(5)]
        for user in users:
            StockAlert.objects.create(product=self.product, user=user, channel=StockAlert.CHANNEL_SMS)
        Product.objects.filter(pk=self.product.pk).update(stock=10)
        self.assertEqual(self.sweep(), 'products_with_pending_alerts=1 notified_products=1')
        self.assertEqual(Notification.objects.filter(template_key='back_in_stock_sms').count(), 6)

    def test_celery_task_delegates_and_is_scheduled_every_ten_minutes(self):
        from holoo.tasks import sweep_stock_alerts as task
        with mock.patch('holoo.stock_alerts.sweep_stock_alerts', return_value='ok') as run:
            self.assertEqual(task(), 'ok')
        run.assert_called_once()
        from pathlib import Path
        from django.conf import settings
        source = (Path(settings.BASE_DIR) / 'config' / 'celery.py').read_text(encoding='utf-8')
        self.assertIn("'holoo.tasks.sweep_stock_alerts'", source)
        self.assertIn('600.0', source)
        self.assertNotIn('check_stock_alerts', source)                     # پرسش دوره‌ای از هلو حذف شد


class AvailabilityBasedTransitionTests(BackInStockEndToEndTests):
    def test_buffer_keeps_a_one_unit_product_unavailable_so_no_early_sms(self):
        from products.models import SiteSettings
        SiteSettings.objects.update_or_create(pk=1, defaults={'stock_safety_buffer': 2})
        cache.delete(SiteSettings.CACHE_KEY)
        self.addCleanup(cache.delete, SiteSettings.CACHE_KEY)
        self.alert()
        self.sync(holoo_item('ERP-BIS-1', few=2))                 # موجودی ۲ − بافر ۲ = قابل‌فروش ۰
        self.assertEqual(self.sms().count(), 0)
        self.sync(holoo_item('ERP-BIS-1', few=3))                 # حالا ۱ قابل‌فروش
        self.assertEqual(self.sms().count(), 1)
