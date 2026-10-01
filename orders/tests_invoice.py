"""
فاکتور سفارش (orders.views.OrderInvoiceView) و فاکتور برگشت از فروش (returns.views.ReturnInvoiceView):
دسترسی، قاعده‌ی سوییچ‌های سربرگ، مهر، ستون‌ها بدون مالیات، جمع‌ها، چاپ، بارکد و دکمه‌های «مشاهده فاکتور».
"""
import re
import shutil
import tempfile
from decimal import Decimal

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from orders.models import Order
from payments.models import Transaction
from products.models import ProductColor, SiteSettings
from returns.models import ReturnItem, ReturnRequest
from returns.tests import ReturnsTestMixin
from services.invoice import code39_svg, seller_details

PNG = ('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==')


class InvoiceBase(ReturnsTestMixin, TestCase):
    def setUp(self):
        self.user = self.make_user()
        self.user.national_code = '0012345678'
        self.user.save()
        self.category = self.make_category()
        self.product = self.make_product(self.category)
        self.product.product_code = '00202010'
        self.product.save()
        self.client.force_login(self.user)
        self.addCleanup(cache.delete, SiteSettings.CACHE_KEY)
        self.set_store(store_name='بازرگانی موسوی', store_legal_name='شرکت موسوی (سهامی خاص)', store_national_id='10320845857',
                       store_registration_number='433845', store_economic_code='411419136511',
                       store_address='قم، بلوار امین، پلاک ۱۲', store_postal_code='3714912345', store_phone_1='02537700000')

    def set_store(self, **fields):
        settings_obj = SiteSettings.load()
        for key, value in fields.items():
            setattr(settings_obj, key, value)
        settings_obj.save()
        cache.delete(SiteSettings.CACHE_KEY)

    def make_paid_order(self, status='delivered', **overrides):
        data = dict(total_price=1164760, shipping_cost=45000, first_name='مریم', last_name='احمدی', phone='09123456789',
                    province='قم', city='قم', address='بلوار پردیسان', postal_code='3749113666')
        data.update(overrides)
        order = self.make_order(self.user, status=status, **data)
        item = self.make_order_item(order, self.product, quantity=2, price=600000)
        item.original_price = Decimal('650000')
        item.discount_amount = Decimal('50000')
        item.save()
        Transaction.objects.create(user=self.user, order=order, amount=order.total_price, authority=f'AUTH-INV-{order.pk}',
                                   status='success', ref_id='704912345')
        return order

    def order_invoice(self, order):
        return self.client.get(reverse('orders:order_invoice', args=[order.pk]))


class OrderInvoiceAccessTests(InvoiceBase):
    def test_owner_gets_the_invoice(self):
        order = self.make_paid_order()
        response = self.order_invoice(order)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'orders/invoice.html')
        self.assertContains(response, 'صورت‌حساب الکترونیکی فروش')

    def test_other_users_order_is_404_and_anonymous_is_redirected(self):
        order = self.make_paid_order()
        self.client.force_login(self.make_user('09140009997'))
        self.assertEqual(self.order_invoice(order).status_code, 404)
        self.client.logout()
        response = self.order_invoice(order)
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response.url)

    def test_canceled_and_unpaid_orders_have_no_invoice(self):
        canceled = self.make_paid_order(status='canceled')
        self.assertEqual(self.order_invoice(canceled).status_code, 404)
        unpaid = self.make_order(self.user, status='pending')
        self.make_order_item(unpaid, self.product)
        self.assertEqual(self.order_invoice(unpaid).status_code, 404)

    def test_paid_pending_and_cheque_shipped_orders_can_be_invoiced(self):
        self.assertEqual(self.order_invoice(self.make_paid_order(status='pending')).status_code, 200)
        cheque = self.make_order(self.user, status='shipped', payment_method='check')
        self.make_order_item(cheque, self.product)
        self.assertEqual(self.order_invoice(cheque).status_code, 200)


class SellerHeaderRuleTests(InvoiceBase):
    """ هر مورد سربرگ فقط وقتی چاپ می‌شود که سوییچش روشن و مقدارش پر باشد """
    SWITCHES = (
        ('invoice_show_national_id', 'store_national_id', 'شناسه ملی:', '10320845857'),
        ('invoice_show_registration_number', 'store_registration_number', 'شماره ثبت:', '433845'),
        ('invoice_show_economic_code', 'store_economic_code', 'شماره اقتصادی:', '411419136511'),
    )

    def seller_html(self, order):
        html = self.order_invoice(order).content.decode()
        return html[html.index('aria-label="فروشنده"'):html.index('aria-label="خریدار"')]

    def test_everything_is_shown_by_default(self):
        seller = self.seller_html(self.make_paid_order())
        self.assertIn('شرکت موسوی (سهامی خاص)', seller)
        for _switch, _field, label, value in self.SWITCHES:
            self.assertIn(label, seller)
            self.assertIn(value, seller)
        for text in ('قم، بلوار امین، پلاک ۱۲', '3714912345', '02537700000'):
            self.assertIn(text, seller)

    def test_each_switch_off_hides_only_its_item(self):
        order = self.make_paid_order()
        for switch, _field, label, value in self.SWITCHES:
            with self.subTest(switch=switch):
                self.set_store(**{switch: False})
                seller = self.seller_html(order)
                self.assertNotIn(label, seller)
                self.assertNotIn(value, seller)
                for other_switch, _f, other_label, _v in self.SWITCHES:
                    if other_switch != switch:
                        self.assertIn(other_label, seller)
                self.set_store(**{switch: True})

    def test_empty_value_hides_the_item_even_with_the_switch_on(self):
        self.set_store(store_economic_code='', store_registration_number='')
        seller = self.seller_html(self.make_paid_order())
        self.assertNotIn('شماره اقتصادی:', seller)
        self.assertNotIn('شماره ثبت:', seller)
        self.assertIn('شناسه ملی:', seller)

    def test_legal_name_off_or_empty_falls_back_to_the_brand_name(self):
        order = self.make_paid_order()
        self.set_store(invoice_show_legal_name=False)
        seller = self.seller_html(order)
        self.assertIn('بازرگانی موسوی', seller)
        self.assertNotIn('شرکت موسوی (سهامی خاص)', seller)
        self.set_store(invoice_show_legal_name=True, store_legal_name='')
        self.assertIn('بازرگانی موسوی', self.seller_html(order))

    def test_seller_details_function_matches_the_rule(self):
        s = SiteSettings.load()
        self.assertEqual(seller_details(s)['national_id'], '10320845857')
        s.invoice_show_national_id = False
        self.assertEqual(seller_details(s)['national_id'], '')
        s.store_stamp_image = ''
        self.assertEqual(seller_details(s)['stamp_url'], '')


class StampTests(InvoiceBase):
    def setUp(self):
        super().setUp()
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=self.media)
        override.enable()
        self.addCleanup(override.disable)

    def upload_stamp(self):
        import base64
        settings_obj = SiteSettings.load()
        settings_obj.store_stamp_image = SimpleUploadedFile('stamp-test.png', base64.b64decode(PNG), content_type='image/png')
        settings_obj.save()
        cache.delete(SiteSettings.CACHE_KEY)

    def test_stamp_is_printed_when_switch_on_and_image_uploaded(self):
        self.upload_stamp()
        html = self.order_invoice(self.make_paid_order()).content.decode()
        self.assertIn('class="inv-stamp"', html)
        self.assertIn('stamp-test', html)

    def test_stamp_is_hidden_when_the_switch_is_off(self):
        self.upload_stamp()
        self.set_store(invoice_show_stamp=False)
        self.assertNotIn('class="inv-stamp"', self.order_invoice(self.make_paid_order()).content.decode())

    def test_no_image_means_no_stamp_even_with_the_switch_on(self):
        self.assertTrue(SiteSettings.load().invoice_show_stamp)
        self.assertNotIn('class="inv-stamp"', self.order_invoice(self.make_paid_order()).content.decode())


class OrderInvoiceContentTests(InvoiceBase):
    def test_buyer_comes_from_the_order_snapshot_and_profile_national_code(self):
        html = self.order_invoice(self.make_paid_order()).content.decode()
        buyer = html[html.index('aria-label="خریدار"'):]
        for text in ('مریم احمدی', '0012345678', '09123456789', '3749113666', 'قم، قم، بلوار پردیسان'):
            self.assertIn(text, buyer)

    def test_item_table_has_no_tax_columns_and_uses_the_separator(self):
        color = ProductColor.objects.create(product=self.product, name='آبی', hex_code='#0000ff')
        order = self.make_paid_order()
        item = order.items.get()
        item.color = color
        item.save()
        html = self.order_invoice(order).content.decode()
        for header in ('ردیف', 'شناسه کالا', 'شرح کالا یا خدمت', 'تعداد', 'مبلغ واحد', 'تخفیف', 'مبلغ کل پس از تخفیف'):
            self.assertIn(header, html)
        for forbidden in ('مالیات', 'عوارض', 'ارزش افزوده'):
            self.assertNotIn(forbidden, html)
        for text in ('650,000', '1,300,000', '100,000', '1,200,000', 'کالای تست', '(آبی)', 'ERP-RETURNS-1', '00202010'):
            self.assertIn(text, html)
        self.assertNotRegex(html, r'(?<![\d,])\d{5,}(\s|<[^>]+>)*تومان')

    def test_summary_footer_payment_and_shipping(self):
        order = self.make_paid_order(shipping_method='courier', order_discount=Decimal('15000'), order_discount_label='کد YALDA')
        Order.objects.filter(pk=order.pk).update(delivered_at=timezone.now())
        html = self.order_invoice(order).content.decode()
        for text in ('کد YALDA', '−15,000', '1,164,760', '45,000', 'روش پرداخت:', 'نقدی', 'تاریخ تحویل:',
                     'مهر و امضای فروشنده', 'مهر و امضای خریدار'):
            self.assertIn(text, html)
        self.assertRegex(html, r'تاریخ تحویل:</b> <span class="inv-num">14\d\d/\d\d/\d\d')

    def test_invoice_number_date_and_tracking(self):
        order = self.make_paid_order(tracking_code='24001234567890')
        html = self.order_invoice(order).content.decode()
        self.assertIn(f'<span class="inv-num">{order.pk}</span>', html)
        self.assertRegex(html, r'تاریخ:</b> <span class="inv-num">14\d\d/\d\d/\d\d')
        # پیگیری = RefID بانکی تراکنش موفق؛ کد رهگیری پستی فقط وقتی چاپ می‌شود که بانکی نباشد (نگاه کنید PaymentAndTrackingTests)
        self.assertIn('پیگیری:</b> <span class="inv-num">704912345</span>', html)

    def test_print_button_standalone_page_and_a4_rules(self):
        from pathlib import Path
        from django.conf import settings
        html = self.order_invoice(self.make_paid_order()).content.decode()
        self.assertIn('data-inv-print', html)                                            # دکمه‌ی چاپ را invoice.js به window.print() وصل می‌کند
        self.assertIn('پرینت / دانلود', html)
        self.assertIn('class="inv-toolbar no-print"', html)
        self.assertNotIn('dashboard', html.lower())                                      # صفحه‌ی مستقل، نه داخل پنل
        css = (Path(settings.BASE_DIR) / 'static/theme/assets/css/invoice.css').read_text(encoding='utf-8')
        for rule in ('@page { margin: 5mm; }', '@media print', '.no-print { display: none !important; }'):
            self.assertIn(rule, css)
        self.assertNotIn('size: A4', css)                                                # اندازه‌ی کاغذ را کاربر در دیالوگ چاپ انتخاب می‌کند

    def test_code_column_is_two_lines_holoo_code_bold_then_long_system_id_small(self):
        html = self.order_invoice(self.make_paid_order()).content.decode()
        self.assertIn('کد / شناسه کالا', html)
        # خط اول: کد کالای عددی (بولد)، خط دوم: شناسه‌ی سیستمی بلند (ریز و خاکستری)
        self.assertIn('<span class="inv-code"><b class="inv-code-main inv-num">00202010</b>'
                      '<span class="inv-code-id inv-num">ERP-RETURNS-1</span></span>', html)
        self.assertLess(html.index('inv-code-main'), html.index('inv-code-id inv-num'))

    def test_missing_product_code_shows_a_dash_not_the_long_id_twice(self):
        self.product.product_code = None
        self.product.save()
        html = self.order_invoice(self.make_paid_order()).content.decode()
        self.assertIn('<b class="inv-code-main inv-num">-</b><span class="inv-code-id inv-num">ERP-RETURNS-1</span>', html)

    def test_description_column_gets_35_to_40_percent_of_the_table(self):
        order = self.make_paid_order()
        html = self.order_invoice(order).content.decode()
        widths = [int(w) for w in re.findall(r'<col style="width:(\d+)%">', html)]
        self.assertEqual(len(widths), 9)
        self.assertEqual(sum(widths), 100)
        self.assertTrue(35 <= widths[2] <= 40, widths)                                  # ستون سوم = شرح کالا
        self.assertEqual(max(widths), widths[2])                                         # پهن‌ترین ستون جدول

    def test_description_style_and_flexible_print_rules(self):
        from pathlib import Path
        from django.conf import settings
        css = (Path(settings.BASE_DIR) / 'static/theme/assets/css/invoice.css').read_text(encoding='utf-8')
        desc = css[css.index('.inv-table th.inv-desc'):]
        desc = desc[:desc.index('}')]
        for rule in ('text-align: right', 'line-height: 1.4', 'word-break: break-word'):
            self.assertIn(rule, desc)
        self.assertIn('table-layout: fixed', css)
        self.assertIn('@media print and (max-width: 160mm)', css)                         # فشرده‌سازی برای A5
        # داخل برگه (جدول/فوتر/مهر/بارکد) اندازه‌ها em هستند تا با font-size برگه مقیاس شوند، نه px/mm ثابت
        for selector in ('.inv-stamp', '.inv-barcode-svg', '.inv-box {'):
            block = css[css.index(selector):]
            block = block[:block.index('}')]
            # حاشیه‌ی 1px مجاز است؛ اندازه، فونت و پدینگ باید نسبی باشند
            self.assertNotRegex(block, r'(font-size|padding|(min-|max-)?(width|height))\s*:[^;]*\d(px|mm)\b')

    def test_return_invoice_has_the_same_code_column_and_description_width(self):
        from returns.models import ReturnItem as RI
        order = self.make_paid_order()
        request = ReturnRequest.objects.create(order=order, user=self.user, refund_method='wallet', status='COMPLETED',
                                               decided_at=timezone.now(), item_received_at=timezone.now(), completed_at=timezone.now())
        RI.objects.create(return_request=request, order_item=order.items.get(), reason=self.make_reason(), requested_quantity=1,
                          approved_quantity=1, refund_amount=Decimal('500000'))
        html = self.client.get(reverse('returns:invoice', args=[request.pk])).content.decode()
        widths = [int(w) for w in re.findall(r'<col style="width:(\d+)%">', html)]
        self.assertEqual((len(widths), sum(widths)), (8, 100))
        self.assertTrue(35 <= widths[2] <= 40, widths)
        self.assertIn('کد / شناسه کالا', html)
        self.assertIn('<b class="inv-code-main inv-num">00202010</b>', html)
        self.assertIn('class="inv-code-id inv-num"', html)

    def test_toolbar_has_the_two_layout_options_and_the_script(self):
        html = self.order_invoice(self.make_paid_order()).content.decode()
        self.assertIn('data-inv-layout="continuous" aria-pressed="true"', html)            # پیش‌فرض: حالت پیوسته
        self.assertIn('data-inv-layout="paged" aria-pressed="false"', html)
        self.assertIn('حالت پیوسته (یکپارچه)', html)
        self.assertIn('برگه‌های مستقل (گسسته)', html)
        self.assertNotIn('A5 /', html)                                                   # بدون برچسب‌های گیج‌کننده‌ی کاغذ
        self.assertNotIn('A4 عمودی', html)
        self.assertIn('theme/assets/js/invoice.js', html)

    def test_server_sends_one_complete_sheet_with_all_rows_and_the_data_for_the_script(self):
        order = self.make_paid_order()
        for index in range(5):
            product = self.make_product(self.category, name=f'کالای اضافه {index}', slug=f'inv-extra-{index}', erp_code=f'ERP-X-{index}')
            self.make_order_item(order, product, quantity=1, price=10000)
        html = self.order_invoice(order).content.decode()
        self.assertEqual(html.count('class="invoice-sheet"'), 1)                          # تقسیم به برگه‌ها کار مرورگر است
        self.assertEqual(html.count('data-row="'), 6)                                     # هر ۶ قلم در همان یک جدول
        self.assertIn('id="invoiceRoot"', html)
        self.assertIn('data-rows-per-sheet="8"', html)                                   # تا ۸ قلم شکسته نمی‌شود
        self.assertNotIn('data-overflow-rows', html)                                     # دیگر آستانه‌ی خودکاری وجود ندارد
        for hook in ('data-inv-head', 'data-inv-body', 'data-inv-foot', 'data-inv-table', 'data-inv-summary', 'data-total-row',
                     'data-total-label', 'data-inv-pageno'):
            self.assertIn(hook, html)
        # مقدار خام هر ردیف (برای جمع مجزای هر برگه در JS) بدون جداکننده و درست
        self.assertIn('data-sum="after" data-value="1200000"', html)
        self.assertIn('data-sum="discount" data-value="100000"', html)
        self.assertIn('data-sum="q" data-value="2"', html)
        for key in ('q', 'total', 'discount', 'after'):
            self.assertIn(f'data-sum-key="{key}"', html)

    def test_each_row_value_attribute_matches_what_is_displayed(self):
        html = self.order_invoice(self.make_paid_order()).content.decode()
        self.assertIn('data-sum="total" data-value="1300000">1,300,000<', html)

    def test_print_css_keeps_every_sheet_on_one_page_without_viewport_units(self):
        from pathlib import Path
        from django.conf import settings
        css = (Path(settings.BASE_DIR) / 'static/theme/assets/css/invoice.css').read_text(encoding='utf-8')
        printed = css[css.index('@page'):]
        for rule in ('.invoice-sheet { font-size: 11px', 'page-break-after: always; break-after: page;',
                     '.invoice-sheet:last-child { page-break-after: auto; break-after: auto; }',
                     'break-inside: avoid !important; page-break-inside: avoid !important;',
                     'height: auto !important; min-height: 0 !important;', 'box-sizing: border-box !important'):
            self.assertIn(rule, printed)
        # در چاپ هیچ واحد vh/vw نیست (کروم آن را برابر ارتفاع کل صفحه می‌گیرد و فوتر را به صفحه‌ی بعد می‌اندازد)
        self.assertNotRegex(printed, r'\d\s*v[hw]')
        self.assertNotIn('98vh', css)

    def test_print_css_is_full_width_without_outer_margins(self):
        from pathlib import Path
        from django.conf import settings
        css = (Path(settings.BASE_DIR) / 'static/theme/assets/css/invoice.css').read_text(encoding='utf-8')
        printed = css[css.index('@page'):]
        self.assertIn('@media screen and (max-width: 720px)', css)                      # چیدمان موبایل در چاپ کاغذ باریک اعمال نشود
        self.assertNotIn('@media (max-width: 720px)', css)
        self.assertIn('html, body { margin: 0 !important; padding: 0 !important;', printed)
        self.assertIn('body, #invoiceRoot, .inv-pages, .invoice-sheet {\n    width: 100% !important; max-width: 100% !important; '
                      'margin: 0 !important; padding: 0 !important; box-sizing: border-box !important;', printed)
        self.assertIn('table { width: 100% !important; }', printed)

    def test_print_fonts_are_readable_at_every_paper_and_rows_have_room(self):
        from pathlib import Path
        from django.conf import settings
        css = (Path(settings.BASE_DIR) / 'static/theme/assets/css/invoice.css').read_text(encoding='utf-8')
        printed = css[css.index('@page'):]
        sizes = [float(v) for v in re.findall(r'\.invoice-sheet\s*\{\s*font-size:\s*([\d.]+)px', printed)]
        self.assertEqual(sorted(sizes), [9.5, 9.8, 10.5, 11.0])                          # A5 افقی، A5 عمودی، A4 افقی، A4 عمودی
        self.assertGreaterEqual(min(sizes), 9.5)
        self.assertNotIn('7.5px', printed)
        self.assertIn('padding: 4px 3px', printed)                                       # سطرهای بهینه تا ۱۰ ردیف در یک A4 جا شود
        # فوتر و جمع نهایی هرگز وسط شکسته نمی‌شوند و فوتر تنها به صفحه‌ی بعد نمی‌افتد
        self.assertIn('.inv-party, .inv-foot, .inv-footer, .inv-summary, .inv-box, .inv-total-row, .inv-table tr, .inv-table thead { break-inside: avoid; page-break-inside: avoid; }', printed)
        self.assertIn('.inv-summary, .inv-foot { break-before: avoid; page-break-before: avoid; }', printed)
        self.assertIn('@media print and (orientation: landscape)', printed)
        self.assertIn('@media print and (orientation: landscape) and (max-width: 220mm)', printed)    # A5 افقی
        self.assertIn('.inv-table th { background: #e2e8f0; color: #000; font-weight: 700;', printed)  # سربرگ ستون بولد و پررنگ
        self.assertIn('.inv-footer { grid-template-columns: repeat(4, 1fr); }', printed)    # فوتر تمام‌عرض

    def test_script_has_no_automatic_print_behaviour_only_two_user_chosen_layouts(self):
        from pathlib import Path
        from django.conf import settings
        js = (Path(settings.BASE_DIR) / 'static/theme/assets/js/invoice.js').read_text(encoding='utf-8')
        code = re.sub(r'/\*.*?\*/', '', js, flags=re.S)                                   # توضیحات حساب نمی‌شوند
        for forbidden in ('onbeforeprint', 'onafterprint', 'beforeprint', 'afterprint', 'matchMedia', 'localStorage', 'orientation'):
            self.assertNotIn(forbidden, code)
        self.assertNotIn('fetch(', code)                                                 # بدون هیچ درخواست جدید به سرور
        self.assertNotIn('XMLHttpRequest', code)
        for text in ('rowsPerSheet', '|| 8;', 'balancedSizes', 'rowPadding', '--inv-row-pad', "'جمع این برگه'", "'جمع کل فاکتور'", 'cloneNode(true)', 'window.print()'):
            self.assertIn(text, js)
        # حالت پیوسته دقیقاً HTML اصلی سرور را برمی‌گرداند و دست‌کاری‌اش نمی‌کند
        self.assertIn('root.innerHTML = pristine;', js)

    def test_return_invoice_has_the_same_dynamic_hooks(self):
        from returns.models import ReturnItem as RI
        order = self.make_paid_order()
        request = ReturnRequest.objects.create(order=order, user=self.user, refund_method='wallet', status='COMPLETED',
                                               decided_at=timezone.now(), item_received_at=timezone.now(), completed_at=timezone.now())
        RI.objects.create(return_request=request, order_item=order.items.get(), reason=self.make_reason(), requested_quantity=1,
                          approved_quantity=1, refund_amount=Decimal('500000'))
        html = self.client.get(reverse('returns:invoice', args=[request.pk])).content.decode()
        for hook in ('id="invoiceRoot"', 'data-row="1"', 'data-sum="amount" data-value="500000"', 'data-sum-key="amount"',
                     'data-total-row', 'data-inv-summary', 'data-inv-layout="paged"'):
            self.assertIn(hook, html)

    def test_barcode_is_embedded_for_the_invoice_number(self):
        order = self.make_paid_order()
        html = self.order_invoice(order).content.decode()
        self.assertIn('class="inv-barcode-svg"', html)
        self.assertIn(f'aria-label="بارکد {order.pk}"', html)


class PaymentAndTrackingTests(InvoiceBase):
    """ «روش پرداخت» بدون «(قیمت N)»، و «پیگیری» هرگز متن «کیف پول» نیست و خالی هم نمی‌ماند """
    def meta(self, order):
        html = self.order_invoice(order).content.decode()
        footer = html[html.index('<section class="inv-footer">'):]
        payment = re.search(r'روش پرداخت:</b> ([^<]*)</p>', footer).group(1)
        tracking = re.search(r'پیگیری:</b> <span class="inv-num">([^<]*)</span>', html)
        return payment, (tracking.group(1) if tracking else None), html

    def order_with(self, payment_method='cash', status='delivered', tracking_code=None, **txn):
        order = self.make_order(self.user, status=status, payment_method=payment_method, total_price=500000, tracking_code=tracking_code)
        self.make_order_item(order, self.product, quantity=1, price=500000)
        if txn is not None:
            data = dict(user=self.user, order=order, amount=Decimal('500000'), authority=f'AUTH-T-{order.pk}', status='success')
            data.update(txn)
            Transaction.objects.create(**data)
        return order

    def test_payment_method_titles_are_clean_for_every_method(self):
        for method, title in (('cash', 'نقدی'), ('check', 'چکی'), ('vip', 'ویژه')):
            with self.subTest(method=method):
                payment, _tracking, html = self.meta(self.order_with(method, ref_id='555'))
                self.assertEqual(payment, title)
                self.assertNotIn('قیمت', html)
                self.assertNotIn('(', payment)

    def test_wallet_only_and_mixed_payment_titles(self):
        wallet_only = self.order_with(amount=Decimal('0'), wallet_amount=Decimal('500000'), ref_id='کیف پول')
        self.assertEqual(self.meta(wallet_only)[0], 'کیف پول')
        mixed = self.order_with(amount=Decimal('300000'), wallet_amount=Decimal('200000'), ref_id='704912')
        self.assertEqual(self.meta(mixed)[0], 'نقدی و کیف پول')

    def test_tracking_is_the_bank_ref_id_for_a_gateway_payment(self):
        _payment, tracking, _html = self.meta(self.order_with(ref_id='862224029'))
        self.assertEqual(tracking, '862224029')

    def test_wallet_payment_never_prints_the_literal_wallet_text_as_tracking(self):
        order = self.order_with(amount=Decimal('0'), wallet_amount=Decimal('500000'), ref_id='کیف پول')
        _payment, tracking, _html = self.meta(order)
        self.assertNotEqual(tracking, 'کیف پول')
        self.assertEqual(tracking, f'سفارش {order.pk}')                                  # بدون رهگیری پستی: مرجع سفارش

    def test_wallet_payment_falls_back_to_the_postal_tracking_code_when_present(self):
        order = self.order_with(amount=Decimal('0'), wallet_amount=Decimal('500000'), ref_id='کیف پول', tracking_code='24001234567890')
        self.assertEqual(self.meta(order)[1], '24001234567890')

    def test_cheque_order_without_any_transaction_still_has_a_tracking_value(self):
        order = self.make_order(self.user, status='shipped', payment_method='check')
        self.make_order_item(order, self.product)
        payment, tracking, _html = self.meta(order)
        self.assertEqual(payment, 'چکی')
        self.assertEqual(tracking, f'سفارش {order.pk}')

    def test_customer_pages_also_hide_the_price_level_in_parentheses(self):
        order = self.order_with('cash', ref_id='1')
        page = self.client.get(reverse('orders:order_detail_full', args=[order.pk])).content.decode()
        self.assertNotIn('قیمت 2', page)
        self.assertNotIn('نقدی (', page)
        self.assertIn('نقدی', page)


class ContinuousPrintBreakingTests(TestCase):
    """ حالت پیوسته باید بتواند بین صفحات تقسیم شود (جدول نباید کامل به صفحه‌ی بعد پرتاب شود) """
    def printed(self):
        from pathlib import Path
        from django.conf import settings
        css = (Path(settings.BASE_DIR) / 'static/theme/assets/css/invoice.css').read_text(encoding='utf-8')
        return css[css.index('@page'):]

    def rule(self, css, selector):
        """ بدنه‌ی اولین قاعده‌ای که دقیقاً با این انتخابگر شروع می‌شود """
        start = css.index('\n  ' + selector + ' {')
        return re.sub(r'/\*.*?\*/', '', css[start:css.index('}', start)], flags=re.S)       # بدون توضیحات

    def test_continuous_sheet_and_table_are_breakable(self):
        printed = self.printed()
        sheet = self.rule(printed, '.invoice-sheet')
        self.assertNotIn('break-inside', sheet)                                          # روی کل برگه‌ی پیوسته ممنوع نیست
        self.assertNotRegex(printed, r'\.inv-table\s*\{[^}]*break-inside\s*:\s*avoid')   # روی کل جدول هم نیست
        self.assertIn('display: block !important', sheet)                                # flex در چاپ تکه‌ناپذیر است

    def test_only_rows_footer_and_totals_are_unbreakable(self):
        printed = self.printed()
        avoid = re.search(r'([^{}]+)\{\s*break-inside: avoid; page-break-inside: avoid;\s*\}', printed).group(1)
        selectors = [x.strip() for x in avoid.split(',')]
        self.assertIn('.inv-table tr', selectors)
        self.assertIn('.inv-foot', selectors)
        self.assertIn('.inv-total-row', selectors)
        self.assertNotIn('.inv-table', selectors)
        self.assertNotIn('.invoice-sheet', selectors)
        self.assertIn('.inv-table thead { display: table-header-group; }', printed)      # تکرار سرستون‌ها در صفحه‌ی بعد

    def test_only_independent_sheets_are_unbreakable_as_a_whole(self):
        printed = self.printed()
        self.assertIn('.is-paged .invoice-sheet { break-inside: avoid !important; page-break-inside: avoid !important; }', printed)


class PagedCapacityTests(TestCase):
    """ ظرفیت برگه‌های مستقل: تا ۸ قلم در هر برگه، تقسیم متوازن، و برگه‌ها نیمه‌خالی نمی‌شوند (۱۳ قلم = ۷ + ۶، نه سه برگه) """
    def run_js(self, expression):
        import json
        import shutil
        import subprocess
        from pathlib import Path
        from django.conf import settings
        node = shutil.which('node')
        if not node:
            self.skipTest('node در دسترس نیست')
        js = (Path(settings.BASE_DIR) / 'static/theme/assets/js/invoice.js').read_text(encoding='utf-8')
        funcs = ''.join(re.search(pattern, js, flags=re.S).group(0) for pattern in (
            r'function rowPadding\(maxRows\) \{.*?\n    \}\n', r'function balancedSizes\(total, perSheet\) \{.*?\n    \}\n'))
        out = subprocess.run([node, '-e', funcs + f'console.log(JSON.stringify({expression}))'], capture_output=True, text=True, check=True)
        return json.loads(out.stdout)

    def test_the_default_capacity_is_eight_rows_per_sheet(self):
        from pathlib import Path
        from django.conf import settings
        layout = (Path(settings.BASE_DIR) / 'templates/invoices/layout.html').read_text(encoding='utf-8')
        self.assertIn('data-rows-per-sheet="8"', layout)

    def test_thirteen_rows_become_two_full_sheets_seven_and_six(self):
        self.assertEqual(self.run_js('balancedSizes(13, 8)'), [7, 6])

    def test_balanced_sizes_never_exceed_capacity_and_use_the_fewest_sheets(self):
        cases = {9: [5, 4], 10: [5, 5], 12: [6, 6], 14: [7, 7], 15: [8, 7], 16: [8, 8], 17: [6, 6, 5], 24: [8, 8, 8]}
        for total, expected in cases.items():
            with self.subTest(total=total):
                self.assertEqual(self.run_js(f'balancedSizes({total}, 8)'), expected)
                self.assertEqual(sum(expected), total)
                self.assertTrue(all(size <= 8 for size in expected))

    def test_row_padding_grows_when_a_sheet_has_few_rows(self):
        pads = [self.run_js(f'rowPadding({n})') for n in (3, 4, 5, 6, 7, 8)]
        self.assertEqual(pads, sorted(pads, reverse=True))
        self.assertGreater(pads[0], pads[-1])

    def test_css_stretches_paged_a4_portrait_and_compacts_small_papers_for_eight_rows(self):
        from pathlib import Path
        from django.conf import settings
        css = (Path(settings.BASE_DIR) / 'static/theme/assets/css/invoice.css').read_text(encoding='utf-8')
        portrait = css[css.index('@media print and (orientation: portrait) and (min-width: 161mm)'):]
        portrait = portrait[:portrait.index('\n}')]
        for rule in ('.is-paged .invoice-sheet { display: flex !important;', 'min-height: 245mm !important',
                     '.is-paged .inv-body .inv-table { flex: 1 1 auto; }', 'padding: var(--inv-row-pad, 4px) 3px'):
            self.assertIn(rule, portrait)
        self.assertNotRegex(portrait, r'\d\s*v[hw]\b')                                   # mm ثابت و امن، نه vh
        landscape_a5 = css[css.index('@media print and (orientation: landscape) and (max-width: 220mm)'):]
        landscape_a5 = landscape_a5[:landscape_a5.index('\n}\n')]
        self.assertIn('.inv-party-body { display: flex; flex-wrap: wrap;', landscape_a5)   # خطوط کوتاه سربرگ کنار هم
        self.assertIn('.inv-summary th, .inv-summary td { padding: 1px 4px; }', landscape_a5)


class BarcodeTests(TestCase):
    def test_code39_structure(self):
        svg = str(code39_svg('120'))
        # شروع/پایان «*» + ۳ رقم = ۵ کاراکتر؛ هرکدام ۵ بار
        self.assertEqual(svg.count('<rect'), 5 * 5)
        self.assertTrue(svg.startswith('<svg'))

    def test_only_digits_are_accepted(self):
        with self.assertRaises(ValueError):
            code39_svg('AB1')

    def test_every_pattern_has_three_wide_elements(self):
        from services.invoice import _CODE39
        for char, pattern in _CODE39.items():
            with self.subTest(char=char):
                self.assertEqual(len(pattern), 9)
                self.assertEqual(pattern.count('w'), 3)


class ReturnInvoiceTests(InvoiceBase):
    def make_return(self, status):
        order = self.make_paid_order()
        reason = self.make_reason(title='مغایرت با تصویر')
        extra = {}
        if status in (ReturnRequest.STATUS_REFUND_PENDING, ReturnRequest.STATUS_COMPLETED):
            extra = {'decided_at': timezone.now(), 'item_received_at': timezone.now(), 'shipping_refunded': True,
                     'shipping_refund_amount': Decimal('45000')}
        if status == ReturnRequest.STATUS_COMPLETED:
            extra['completed_at'] = timezone.now()
        request = self.make_return_request(order, self.user, status=status, **extra)
        ReturnItem.objects.create(
            return_request=request, order_item=order.items.get(), reason=reason, requested_quantity=1,
            approved_quantity=1 if extra else None, refund_amount=Decimal('550000') if extra else None,
        )
        return request

    def invoice(self, request):
        return self.client.get(reverse('returns:invoice', args=[request.pk]))

    def test_invoice_exists_once_amounts_are_final(self):
        for status in (ReturnRequest.STATUS_REFUND_PENDING, ReturnRequest.STATUS_COMPLETED):
            with self.subTest(status=status):
                ReturnRequest.objects.all().delete()
                response = self.invoice(self.make_return(status))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'صورت‌حساب الکترونیکی برگشت از فروش')

    def test_no_invoice_before_inspection_or_after_rejection(self):
        for status in (ReturnRequest.STATUS_PENDING, ReturnRequest.STATUS_APPROVED, ReturnRequest.STATUS_ITEM_RECEIVED):
            with self.subTest(status=status):
                ReturnRequest.objects.all().delete()
                self.assertEqual(self.invoice(self.make_return(status)).status_code, 404)

    def test_other_users_request_is_404_and_anonymous_is_redirected(self):
        request = self.make_return(ReturnRequest.STATUS_COMPLETED)
        self.client.force_login(self.make_user('09140009996'))
        self.assertEqual(self.invoice(request).status_code, 404)
        self.client.logout()
        self.assertEqual(self.invoice(request).status_code, 302)

    def test_content_amounts_reference_and_seller_rules(self):
        request = self.make_return(ReturnRequest.STATUS_COMPLETED)
        self.set_store(invoice_show_economic_code=False)
        html = self.invoice(request).content.decode()
        for text in ('مغایرت با تصویر', '550,000', '45,000', '595,000', 'مبلغ کل برگشت از فروش', 'بازگشت هزینه‌ی ارسال سفارش',
                     'شماره فاکتور فروش:', 'شرکت موسوی (سهامی خاص)', 'مریم احمدی', 'class="inv-barcode-svg"'):
            self.assertIn(text, html)
        self.assertNotIn('شماره اقتصادی:', html)
        for forbidden in ('مالیات', 'عوارض', 'ارزش افزوده'):
            self.assertNotIn(forbidden, html)

    def test_return_detail_button_is_a_link_only_when_the_invoice_exists(self):
        done = self.make_return(ReturnRequest.STATUS_COMPLETED)
        page = self.client.get(reverse('returns:detail', args=[done.pk])).content.decode()
        self.assertIn(reverse('returns:invoice', args=[done.pk]), page)
        ReturnRequest.objects.all().delete()
        pending = self.make_return(ReturnRequest.STATUS_PENDING)
        page = self.client.get(reverse('returns:detail', args=[pending.pk])).content.decode()
        self.assertNotIn(reverse('returns:invoice', args=[pending.pk]), page)


class InvoiceButtonTests(InvoiceBase):
    def test_order_detail_button_is_a_link_for_invoiceable_orders_and_disabled_otherwise(self):
        order = self.make_paid_order()
        page = self.client.get(reverse('orders:order_detail_full', args=[order.pk])).content.decode()
        self.assertIn(reverse('orders:order_invoice', args=[order.pk]), page)
        unpaid = self.make_order(self.user, status='pending')
        self.make_order_item(unpaid, self.product)
        page = self.client.get(reverse('orders:order_detail_full', args=[unpaid.pk])).content.decode()
        self.assertNotIn(reverse('orders:order_invoice', args=[unpaid.pk]), page)
        self.assertRegex(page, r'<button type="button" class="od-btn" disabled title="[^"]+">\s*<svg[^>]*>.*?</svg>\s*مشاهده فاکتور')

    def test_history_shows_the_invoice_link_only_on_delivered_cards(self):
        delivered = self.make_paid_order(status='delivered')
        shipped = self.make_paid_order(status='shipped')
        tab = self.client.get(reverse('orders:order_history'), {'tab': 'delivered'}).content.decode()
        self.assertIn(reverse('orders:order_invoice', args=[delivered.pk]), tab)
        current = self.client.get(reverse('orders:order_history')).content.decode()
        self.assertNotIn(reverse('orders:order_invoice', args=[shipped.pk]), current)
