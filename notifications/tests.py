"""تست لایه‌ی اطلاع‌رسانی — قابل تعویض بودن موتور و گم‌نشدن پیام."""

from unittest import mock

from django.apps import apps
from django.core.cache import cache
from django.test import TestCase, override_settings

from accounts.models import CustomUser
from notifications.backends.base import NotificationBackend, NotificationBackendError
from notifications.backends.melipayamak import MelipayamakBackend
from notifications.models import Notification, NotificationSetting
from notifications.service import deliver, get_backend, notify, notify_admin
from notifications.templates_registry import TEMPLATES, render_message
from products.models import SiteSettings


def use_backend(test_case, path):
    """
    موتور ارسال فعال را برای طول یک تست عوض می‌کند.

    از SiteSettings.notification_backend استفاده می‌کند (نه override_settings) چون
    get_backend() حالا این مقدار را از تنظیمات سایت می‌خواند، نه از settings.py. کش
    Redis این تنظیمات با rollback تراکنش تست پاک نمی‌شود، پس صریح پاکش می‌کنیم.
    """
    settings_obj = SiteSettings.load()
    settings_obj.notification_backend = path
    settings_obj.save()
    test_case.addCleanup(cache.delete, SiteSettings.CACHE_KEY)


class CaptureBackend(NotificationBackend):
    """ موتور تستی: فقط ثبت می‌کند که چه چیزی به آن داده شده """
    sent = []

    def send(self, to, text):
        CaptureBackend.sent.append((to, text))
        return 'capture-id'


class BrokenBackend(NotificationBackend):
    def send(self, to, text):
        raise NotificationBackendError('سرویس در دسترس نیست')


CAPTURE = 'notifications.tests.CaptureBackend'
BROKEN = 'notifications.tests.BrokenBackend'


class TemplateTests(TestCase):
    def test_renders_with_context(self):
        self.assertEqual(render_message('otp', {'code': '123456'}),
                         'کد تایید شما برای ورود به فروشگاه: 123456')

    def test_missing_parameter_raises(self):
        with self.assertRaises(KeyError):
            render_message('payment_succeeded_customer', {'name': 'علی'})

    def test_unknown_template_raises(self):
        with self.assertRaises(KeyError):
            render_message('does_not_exist', {})


class NotifyTests(TestCase):
    def setUp(self):
        CaptureBackend.sent.clear()
        use_backend(self, CAPTURE)

    def test_nothing_is_sent_before_commit(self):
        with mock.patch('notifications.tasks.deliver_notification.delay') as task:
            with self.captureOnCommitCallbacks(execute=False):
                notify('09120000030', 'otp', code='111111')
            self.assertEqual(task.call_count, 0)

    def test_task_is_fired_after_commit(self):
        with mock.patch('notifications.tasks.deliver_notification.delay') as task:
            with self.captureOnCommitCallbacks(execute=True):
                notify('09120000030', 'otp', code='111111')
        self.assertEqual(task.call_count, 1)

    def test_backend_only_receives_recipient_and_text(self):
        """ قرارداد موتور فقط (مقصد، متن) است — نه سفارش، نه کاربر، نه نوع پیام """
        with mock.patch('notifications.tasks.deliver_notification.delay'):
            notification = notify('09120000030', 'otp', code='111111')
        deliver(notification)

        self.assertEqual(CaptureBackend.sent,
                         [('09120000030', 'کد تایید شما برای ورود به فروشگاه: 111111')])

    def test_successful_delivery_is_recorded(self):
        with mock.patch('notifications.tasks.deliver_notification.delay'):
            notification = notify('09120000030', 'otp', code='111111')
        deliver(notification)
        notification.refresh_from_db()

        self.assertEqual(notification.status, Notification.STATUS_SENT)
        self.assertEqual(notification.attempts, 1)
        self.assertEqual(notification.provider_message_id, 'capture-id')
        self.assertIsNotNone(notification.sent_at)

    def test_missing_parameter_does_not_create_notification(self):
        with mock.patch('notifications.tasks.deliver_notification.delay'):
            result = notify('09120000030', 'payment_succeeded_customer', name='علی')
        self.assertIsNone(result)
        self.assertEqual(Notification.objects.count(), 0)

    def test_empty_recipient_is_skipped(self):
        self.assertIsNone(notify('', 'otp', code='111111'))
        self.assertEqual(Notification.objects.count(), 0)

    @override_settings(ADMIN_NOTIFICATION_RECIPIENT='09199999999')
    def test_notify_admin_uses_configured_recipient(self):
        with mock.patch('notifications.tasks.deliver_notification.delay'):
            notification = notify_admin('critical_alert', message='تست')
        self.assertEqual(notification.recipient, '09199999999')


class BackendSwapTests(TestCase):
    def test_backend_is_selected_from_site_settings(self):
        """ تعویض سرویس ارسال باید با یک تغییر در تنظیمات سایت (کمبوی ادمین) اثر کند """
        use_backend(self, CAPTURE)
        self.assertIsInstance(get_backend(), CaptureBackend)

        use_backend(self, 'notifications.backends.email.EmailBackend')
        from notifications.backends.email import EmailBackend
        self.assertIsInstance(get_backend(), EmailBackend)

    def test_default_backend_is_console(self):
        from notifications.backends.console import ConsoleBackend
        self.assertIsInstance(get_backend(), ConsoleBackend)


class FailureTests(TestCase):
    def setUp(self):
        use_backend(self, BROKEN)

    def test_failure_is_recorded_and_raised(self):
        """ شکست ارسال نباید بی‌سروصدا بلعیده شود؛ باید قابل پیگیری و تلاش مجدد بماند """
        with mock.patch('notifications.tasks.deliver_notification.delay'):
            notification = notify('09120000031', 'otp', code='222222')

        with self.assertRaises(NotificationBackendError):
            deliver(notification)

        notification.refresh_from_db()
        self.assertEqual(notification.status, Notification.STATUS_FAILED)
        self.assertEqual(notification.attempts, 1)
        self.assertIn('در دسترس نیست', notification.error)

    def test_broker_failure_leaves_notification_pending_for_retry(self):
        with mock.patch('notifications.tasks.deliver_notification.delay', side_effect=OSError('redis down')):
            with self.captureOnCommitCallbacks(execute=True):
                notification = notify('09120000032', 'otp', code='333333')

        notification.refresh_from_db()
        self.assertEqual(notification.status, Notification.STATUS_PENDING)


class MelipayamakBackendTests(TestCase):
    """
    ملی‌پیامک دو خط دارد: خط خدماتی اشتراکی (فقط bodyId تاییدشده + متغیرهای مرتب‌شده،
    از طریق send_template) و خط اختصاصی SmartSMS (متن آزاد، از طریق send). قالبی که در
    TEMPLATE_MAP نیست باید بدون خطا از خط اختصاصی برود، نه اینکه شکست بخورد.
    """

    def setUp(self):
        self.backend = MelipayamakBackend()
        self.settings_override = override_settings(
            MELIPAYAMAK_USERNAME='09120000000', MELIPAYAMAK_APIKEY='test-apikey',
            MELIPAYAMAK_FROM_NUMBER='50002710040293',
        )
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

    def _mock_response(self, ret_status=1, value='123456789012345', str_ret_status='Ok'):
        response = mock.Mock(status_code=200)
        response.json.return_value = {'Value': value, 'RetStatus': ret_status, 'StrRetStatus': str_ret_status}
        return response

    def test_send_uses_smart_sms_dedicated_line(self):
        with mock.patch('notifications.backends.melipayamak.requests.post') as post:
            post.return_value = self._mock_response()
            message_id = self.backend.send('09120000030', 'متن آزاد از خط اختصاصی')

        self.assertEqual(message_id, '123456789012345')
        url = post.call_args.args[0]
        payload = post.call_args.kwargs['data']
        self.assertEqual(url, 'https://rest.payamak-panel.com/api/SmartSMS/Send')
        self.assertEqual(payload['text'], 'متن آزاد از خط اختصاصی')
        self.assertEqual(payload['from'], '50002710040293')
        self.assertEqual(payload['to'], '09120000030')

    def test_send_requires_from_number(self):
        with override_settings(MELIPAYAMAK_FROM_NUMBER=''):
            with self.assertRaises(NotificationBackendError):
                self.backend.send('09120000030', 'متن')

    def test_sends_ordered_variables_joined_by_semicolon(self):
        with mock.patch('notifications.backends.melipayamak.requests.post') as post:
            post.return_value = self._mock_response()
            message_id = self.backend.send_template(
                '09120000030', 'payment_succeeded_customer',
                {'name': 'علی', 'amount': '100,000', 'ref_id': 'REF1'}, text='(نادیده گرفته می‌شود)',
            )

        self.assertEqual(message_id, '123456789012345')
        url = post.call_args.args[0]
        payload = post.call_args.kwargs['data']
        self.assertEqual(url, 'https://rest.payamak-panel.com/api/SendSMS/BaseServiceNumber')
        self.assertEqual(payload['bodyId'], 537943)
        self.assertEqual(payload['text'], 'علی;100,000;REF1')
        self.assertEqual(payload['to'], '09120000030')
        self.assertEqual(payload['username'], '09120000000')
        self.assertEqual(payload['password'], 'test-apikey')  # طبق مستندات ملی‌پیامک، apikey جای رمز عبور می‌رود

    def test_unmapped_template_falls_back_to_dedicated_line(self):
        """ critical_alert و holoo_sync_stalled_admin روی خط اشتراکی تایید نشدند؛ باید بدون خطا از خط اختصاصی بروند """
        with mock.patch('notifications.backends.melipayamak.requests.post') as post:
            post.return_value = self._mock_response()
            message_id = self.backend.send_template(
                '09120000030', 'critical_alert', {'message': 'خطا'}, text='🚨 خطای بحرانی در سایت: خطا',
            )

        self.assertEqual(message_id, '123456789012345')
        url = post.call_args.args[0]
        payload = post.call_args.kwargs['data']
        self.assertEqual(url, 'https://rest.payamak-panel.com/api/SmartSMS/Send')
        self.assertEqual(payload['text'], '🚨 خطای بحرانی در سایت: خطا')

    def test_missing_context_variable_raises(self):
        with self.assertRaises(NotificationBackendError):
            self.backend.send_template('09120000030', 'otp', {}, text='...')

    def test_provider_rejection_raises(self):
        with mock.patch('notifications.backends.melipayamak.requests.post') as post:
            post.return_value = self._mock_response(ret_status=35, value='4', str_ret_status='InvalidData')
            with self.assertRaises(NotificationBackendError):
                self.backend.send_template('09120000030', 'otp', {'code': '111111'}, text='...')

    def test_missing_credentials_raises(self):
        with override_settings(MELIPAYAMAK_USERNAME='', MELIPAYAMAK_APIKEY=''):
            with self.assertRaises(NotificationBackendError):
                self.backend.send_template('09120000030', 'otp', {'code': '111111'}, text='...')

    def test_order_templates_are_mapped_to_shared_line(self):
        """ الگوهای سفارش/ارسال پستی روی خط اشتراکی تایید شده‌اند؛ هلو و بحرانی عمداً نیستند """
        from notifications.backends.melipayamak import TEMPLATE_MAP

        self.assertIn('order_placed_customer', TEMPLATE_MAP)
        self.assertIn('order_shipped_customer', TEMPLATE_MAP)
        self.assertNotIn('holoo_sync_stalled_admin', TEMPLATE_MAP)
        self.assertNotIn('critical_alert', TEMPLATE_MAP)

        with mock.patch('notifications.backends.melipayamak.requests.post') as post:
            post.return_value = self._mock_response()
            self.backend.send_template(
                '09120000030', 'order_shipped_customer',
                {'name': 'علی', 'tracking_code': 'POST-1'}, text='...',
            )
        payload = post.call_args.kwargs['data']
        self.assertEqual(payload['bodyId'], 537933)
        self.assertEqual(payload['text'], 'علی;POST-1')


class BackendOverrideTests(TestCase):
    """
    notify(..., backend=...) باید سرویس سراسری تنظیمات سایت را برای همان یک پیام دور بزند —
    لازم برای اطلاع موجودی: کاربر می‌تواند «ایمیل» انتخاب کند حتی وقتی سرویس فعال سایت پیامک است.
    """

    def test_explicit_backend_overrides_site_wide_backend(self):
        use_backend(self, BROKEN)  # سرویس سراسری سایت عمداً یک بک‌اند خراب/بی‌ربط است
        CaptureBackend.sent.clear()
        with mock.patch('notifications.tasks.deliver_notification.delay'):
            notification = notify('to@example.com', 'otp', backend=CAPTURE, code='123456')
        self.assertEqual(notification.backend_override, CAPTURE)

        deliver(notification)  # نباید BROKEN را صدا بزند، وگرنه NotificationBackendError می‌داد
        self.assertEqual(CaptureBackend.sent, [('to@example.com', 'کد تایید شما برای ورود به فروشگاه: 123456')])

    def test_no_backend_argument_uses_site_wide_backend_as_before(self):
        use_backend(self, CAPTURE)
        with mock.patch('notifications.tasks.deliver_notification.delay'):
            notification = notify('09120000099', 'otp', code='999999')
        self.assertEqual(notification.backend_override, '')


class ProductBackInStockReceiverTests(TestCase):
    """ notifications/receivers.py::on_product_back_in_stock """

    def setUp(self):
        from products.models import Category, Product, StockAlert
        self.StockAlert = StockAlert
        self.category = Category.objects.create(name='تست', slug='back-in-stock-test-cat')
        self.product = Product.objects.create(
            name='کالای تست موجودی', slug='back-in-stock-test-product', erp_code='ERP-BACK-IN-STOCK-1',
            category=self.category, price=100000, stock=5,
        )
        self.user = CustomUser.objects.create_user(phone_number='09120000061')

    def _fire(self):
        from products.signals import product_back_in_stock
        from products.models import Product
        product_back_in_stock.send_robust(sender=Product, product=self.product)

    def test_sms_alert_notifies_customer_and_marks_notified(self):
        alert = self.StockAlert.objects.create(product=self.product, user=self.user, channel=self.StockAlert.CHANNEL_SMS)
        with mock.patch('notifications.receivers.notify') as notify_mock:
            self._fire()

        notify_mock.assert_called_once_with(self.user.phone_number, 'back_in_stock_sms', product_name=self.product.name)
        alert.refresh_from_db()
        self.assertEqual(alert.status, self.StockAlert.STATUS_NOTIFIED)
        self.assertIsNotNone(alert.notified_at)

    def test_email_alert_uses_email_backend_override(self):
        self.user.email = 'user@example.com'
        self.user.first_name = 'رضا'
        self.user.save()
        self.StockAlert.objects.create(product=self.product, user=self.user, channel=self.StockAlert.CHANNEL_EMAIL)

        with mock.patch('notifications.receivers.notify') as notify_mock:
            self._fire()

        notify_mock.assert_called_once_with(
            'user@example.com', 'back_in_stock_email',
            backend='notifications.backends.email.EmailBackend',
            name='رضا', product_name=self.product.name,
        )

    def test_email_alert_uses_its_own_email_not_profile_email_when_overridden(self):
        """ کاربری که در همین درخواست ایمیل دیگری داده، باید همان بگیرد نه ایمیل پروفایلش را """
        self.user.email = 'profile@example.com'
        self.user.save()
        self.StockAlert.objects.create(
            product=self.product, user=self.user, channel=self.StockAlert.CHANNEL_EMAIL,
            email='request-only@example.com',
        )

        with mock.patch('notifications.receivers.notify') as notify_mock:
            self._fire()

        self.assertEqual(notify_mock.call_args.args[0], 'request-only@example.com')

    def test_already_notified_alerts_are_not_renotified(self):
        self.StockAlert.objects.create(
            product=self.product, user=self.user, channel=self.StockAlert.CHANNEL_SMS,
            status=self.StockAlert.STATUS_NOTIFIED,
        )
        with mock.patch('notifications.receivers.notify') as notify_mock:
            self._fire()
        self.assertEqual(notify_mock.call_count, 0)

    def test_no_pending_alerts_does_nothing(self):
        with mock.patch('notifications.receivers.notify') as notify_mock:
            self._fire()
        self.assertEqual(notify_mock.call_count, 0)


class NotificationSettingSeedMigrationTests(TestCase):
    """
    notifications/migrations/0005_seed_notification_settings.py - تابع seed مستقیماً صدا زده
    می‌شود (نه اینکه به وجود ردیف‌های seed‌شده در دیتابیس تست تکیه کنیم)؛ چون flush/serialized_rollback
    تست‌های TransactionTestCase دیگر می‌تواند ردیف‌های ساخته‌شده توسط دیتا-مایگریشن را از دیتابیس
    تست بین اجراهای مختلف پاک کند - این خودش رفتار مایگریشن را مستقل از آن تست می‌کند.
    """

    def _run_seed(self):
        import importlib
        module = importlib.import_module('notifications.migrations.0005_seed_notification_settings')
        module.seed_notification_settings(apps, None)

    def setUp(self):
        NotificationSetting.objects.all().delete()

    def test_seed_creates_a_row_per_template_key(self):
        self._run_seed()
        seeded_keys = set(NotificationSetting.objects.values_list('template_key', flat=True))
        self.assertEqual(seeded_keys, set(TEMPLATES.keys()))

    def test_seeded_rows_default_to_enabled_without_custom_body(self):
        self._run_seed()
        setting = NotificationSetting.objects.get(template_key='otp')
        self.assertTrue(setting.is_enabled)
        self.assertEqual(setting.custom_body, '')

    def test_seed_is_idempotent_and_does_not_touch_existing_rows(self):
        self._run_seed()
        NotificationSetting.objects.filter(template_key='otp').update(is_enabled=False, custom_body='دست‌نخورده بماند')
        self._run_seed()
        setting = NotificationSetting.objects.get(template_key='otp')
        self.assertFalse(setting.is_enabled)
        self.assertEqual(setting.custom_body, 'دست‌نخورده بماند')
        self.assertEqual(NotificationSetting.objects.count(), len(TEMPLATES))


class NotificationSettingTests(TestCase):
    """ notify() باید NotificationSetting را برای فعال/غیرفعال و متن جای‌گزین چک کند """

    def setUp(self):
        CaptureBackend.sent.clear()
        use_backend(self, CAPTURE)
        # ردیف‌های seed‌شده‌ی احتمالی از قبل را پاک می‌کنیم تا این تست‌ها مستقل از داده‌ی
        # باقی‌مانده در دیتابیس تست (بین اجراهای مختلف) باشند
        NotificationSetting.objects.all().delete()

    def test_disabled_template_is_not_sent_at_all(self):
        NotificationSetting.objects.create(template_key='otp', is_enabled=False)
        with mock.patch('notifications.tasks.deliver_notification.delay'):
            result = notify('09120000040', 'otp', code='111111')
        self.assertIsNone(result)
        self.assertEqual(Notification.objects.count(), 0)

    def test_enabled_template_without_custom_body_uses_default_text(self):
        NotificationSetting.objects.create(template_key='otp', is_enabled=True)
        with mock.patch('notifications.tasks.deliver_notification.delay'):
            notification = notify('09120000041', 'otp', code='111111')
        self.assertEqual(notification.text, 'کد تایید شما برای ورود به فروشگاه: 111111')

    def test_custom_body_overrides_default_text(self):
        NotificationSetting.objects.create(
            template_key='otp', custom_body='کد ورود اختصاصی شما: {code} - این پیام تست است',
        )
        with mock.patch('notifications.tasks.deliver_notification.delay'):
            notification = notify('09120000042', 'otp', code='222222')
        self.assertEqual(notification.text, 'کد ورود اختصاصی شما: 222222 - این پیام تست است')

    def test_custom_body_still_enforces_required_params(self):
        NotificationSetting.objects.create(
            template_key='payment_succeeded_customer',
            custom_body='{name} گرامی، {amount} تومان با کد {ref_id} پرداخت شد.',
        )
        with mock.patch('notifications.tasks.deliver_notification.delay'):
            result = notify('09120000043', 'payment_succeeded_customer', name='علی', amount='1000')
        self.assertIsNone(result)
        self.assertEqual(Notification.objects.count(), 0)

    def test_template_without_a_setting_row_still_sends_normally(self):
        """ کلیدی که هنوز seed نشده (مثلاً بین دو دیپلوی) نباید notify را بشکند """
        with mock.patch('notifications.tasks.deliver_notification.delay'):
            notification = notify('09120000044', 'otp', code='333333')
        self.assertIsNotNone(notification)
        self.assertEqual(notification.text, 'کد تایید شما برای ورود به فروشگاه: 333333')


class NotificationSettingTitleAndGuideTests(TestCase):
    """
    رفع مشکل UI که ادمین دیده بود: کلید انگلیسی به‌جای عنوان فارسی، و کادر متن جای‌گزین بدون
    راهنمای متن پیش‌فرض/متغیرها. این‌ها همیشه از TEMPLATES خوانده می‌شوند (نه کپی‌شده در دیتابیس)
    تا با تغییر کد بدون مایگریشن هم‌سو بمانند.
    """

    def test_title_comes_from_templates_registry(self):
        setting = NotificationSetting(template_key='otp')
        self.assertEqual(setting.title, 'کد تأیید ورود (OTP)')

    def test_default_body_and_variables_come_from_templates_registry(self):
        setting = NotificationSetting(template_key='payment_succeeded_customer')
        self.assertEqual(setting.default_body, TEMPLATES['payment_succeeded_customer'].body)
        self.assertEqual(setting.available_variables, ('name', 'amount', 'ref_id'))

    def test_unknown_key_falls_back_to_the_key_itself(self):
        """ کلیدی که دیگر در کد نیست (مثلاً بعد از حذف یک قالب) نباید صفحه‌ی ادمین را بشکند """
        setting = NotificationSetting(template_key='a_removed_template_key')
        self.assertEqual(setting.title, 'a_removed_template_key')
        self.assertEqual(setting.default_body, '')
        self.assertEqual(setting.available_variables, ())

    def test_str_uses_title(self):
        setting = NotificationSetting(template_key='otp')
        self.assertEqual(str(setting), 'کد تأیید ورود (OTP)')


class NotificationSettingAdminTests(TestCase):
    def setUp(self):
        self.admin_user = CustomUser.objects.create_superuser(phone_number='09120000998')
        self.client.force_login(self.admin_user)
        self.setting, _ = NotificationSetting.objects.get_or_create(template_key='otp')

    def test_add_permission_is_disabled(self):
        response = self.client.get('/admin/notifications/notificationsetting/add/')
        self.assertEqual(response.status_code, 403)

    def test_delete_permission_is_disabled(self):
        response = self.client.post('/admin/notifications/notificationsetting/', {
            'action': 'delete_selected', '_selected_action': [self.setting.pk], 'post': 'yes',
        })
        self.assertEqual(response.status_code, 200)   # اکشن اصلاً در لیست نیست؛ فرم دوباره رندر می‌شود
        self.assertTrue(NotificationSetting.objects.filter(pk=self.setting.pk).exists())

    def test_changelist_and_change_pages_render(self):
        for url in ('/admin/notifications/notificationsetting/', f'/admin/notifications/notificationsetting/{self.setting.pk}/change/'):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_changelist_shows_persian_title_not_just_the_raw_key(self):
        response = self.client.get('/admin/notifications/notificationsetting/')
        self.assertContains(response, 'کد تأیید ورود (OTP)')

    def test_change_form_shows_default_body_and_variables_guide(self):
        setting, _ = NotificationSetting.objects.get_or_create(template_key='payment_succeeded_customer')
        response = self.client.get(f'/admin/notifications/notificationsetting/{setting.pk}/change/')
        self.assertContains(response, 'مشتری گرامی {name}، پرداخت مبلغ {amount} تومان')
        self.assertContains(response, '{name}')
        self.assertContains(response, '{amount}')
        self.assertContains(response, '{ref_id}')
