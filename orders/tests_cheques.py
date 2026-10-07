"""
فاز B چکی: ثبت اطلاعات چک (شناسه‌ی صیادی ۱۶ رقمی + ۱ تا ۵ تصویر) برای سفارش چکی، چندچکی، امنیت تصویر (magic bytes، حذف EXIF، فقط
JPG/PNG/WebP، نام uuid، بدون نشانی عمومی)، کنترل دسترسی (صاحب سفارش و ادمین)، پاک‌سازی فایل نیمه‌کاره و محدودیت‌ها.
MEDIA_ROOT برای هر تست یک پوشه‌ی موقت است؛ هیچ فایلی به media/ واقعی نمی‌رود.
"""
import io
import os
import shutil
import tempfile
from datetime import date
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from PIL import Image

from accounts.testing import make_approved_user
from orders import cheques
from orders.models import ChequeImage, ChequePayment, Order
from orders.tests import CheckoutTestBase
from products.models import SiteSettings


# ------------------------------------------------------------------ نمونه‌ها
def make_image(fmt='JPEG', size=(40, 30), *, exif=False, orientation=None, trailing=b''):
    image = Image.new('RGB', size, (200, 40, 40))
    buffer = io.BytesIO()
    kwargs = {}
    if fmt == 'JPEG' and (exif or orientation):
        tags = Image.Exif()
        if exif:
            tags[0x010E] = 'SECRET-GPS-123'
        if orientation:
            tags[0x0112] = orientation
        kwargs['exif'] = tags.tobytes()
    image.save(buffer, fmt, **kwargs)
    return buffer.getvalue() + trailing


def upload(data, name='cheque.jpg', content_type='image/jpeg'):
    return SimpleUploadedFile(name, data, content_type=content_type)


def stored_files(root):
    found = []
    for folder, _, names in os.walk(os.path.join(root, 'cheque_images')):
        found += [os.path.join(folder, n) for n in names]
    return found


VALID = '6219861012345678'


class MediaIsolation:
    """ MEDIA_ROOT موقت برای هر تست + پاک‌سازی کش (سقف نرخ) """

    def setUp(self):
        super().setUp()
        self.media_root = tempfile.mkdtemp(prefix='cheque-media-')
        self.addCleanup(shutil.rmtree, self.media_root, True)
        self.enterContext(override_settings(MEDIA_ROOT=self.media_root))
        cache.clear()
        self.addCleanup(cache.clear)
        self.addCleanup(cache.delete, SiteSettings.CACHE_KEY)


# ------------------------------------------------------------------ اعتبارسنجی فیلدها
class FieldParsingTests(TestCase):
    def errors(self, func, *args):
        with self.assertRaises(cheques.ChequeError) as caught:
            func(*args)
        return caught.exception.errors

    def test_the_sayadi_id_accepts_persian_digits_and_separators(self):
        self.assertEqual(cheques.normalize_sayadi('۶۲۱۹۸۶۱۰۱۲۳۴۵۶۷۸'), VALID)
        self.assertEqual(cheques.normalize_sayadi(' 6219-8610 1234 5678 '), VALID)
        self.assertEqual(cheques.normalize_sayadi('٦٢١٩٨٦١٠١٢٣٤٥٦٧٨'), VALID)

    def test_a_bad_sayadi_id_is_rejected(self):
        for bad in ('', '   ', '123', VALID + '9', 'abcdefghijklmnop', '6219861012345 78x'):
            with self.subTest(bad=bad):
                self.assertIn('sayadi_id', self.errors(cheques.normalize_sayadi, bad))

    def test_amount(self):
        self.assertIsNone(cheques.parse_amount(''))
        self.assertEqual(cheques.parse_amount('۱٬۲۰۰٬۰۰۰'), 1200000)
        self.assertEqual(cheques.parse_amount('1,500,000'), 1500000)
        for bad in ('0', '-5', 'abc', '1.5', str(10 ** 13)):
            with self.subTest(bad=bad):
                self.assertIn('amount', self.errors(cheques.parse_amount, bad))

    def test_due_date_in_jalali_or_iso(self):
        self.assertIsNone(cheques.parse_due_date(''))
        self.assertEqual(cheques.parse_due_date('1405/08/15'), date(2026, 11, 6))
        self.assertEqual(cheques.parse_due_date('۱۴۰۵/۰۸/۱۵'), date(2026, 11, 6))
        self.assertEqual(cheques.parse_due_date('2026-11-06'), date(2026, 11, 6))
        for bad in ('1405/13/01', '1405/02/31x', 'سلام', '1405/08'):
            with self.subTest(bad=bad):
                self.assertIn('due_date', self.errors(cheques.parse_due_date, bad))

    def test_free_text_is_collapsed_and_length_limited(self):
        self.assertEqual(cheques.clean_text('  بانک \n\t ملی\x00 ', 60, 'bank_name', 'نام بانک'), 'بانک ملی')
        self.assertIn('bank_name', self.errors(cheques.clean_text, 'x' * 61, 60, 'bank_name', 'نام بانک'))


# ------------------------------------------------------------------ امنیت تصویر
class ImageSecurityTests(MediaIsolation, TestCase):
    def prep(self, *files):
        return cheques.prepare_images(list(files))

    def error(self, *files):
        with self.assertRaises(cheques.ChequeError) as caught:
            self.prep(*files)
        return caught.exception.errors['images']

    def test_real_images_are_accepted_and_typed_by_content(self):
        for fmt, ctype, ext in (('JPEG', 'image/jpeg', 'jpg'), ('PNG', 'image/png', 'png'), ('WEBP', 'image/webp', 'webp')):
            with self.subTest(fmt=fmt):
                item = self.prep(upload(make_image(fmt), name='x.bin', content_type='application/octet-stream'))[0]
                self.assertEqual((item.content_type, item.ext), (ctype, ext))

    def test_spoofed_and_forbidden_files_are_rejected(self):
        samples = {
            'php-as-jpg': upload(b'<?php system($_GET["c"]); ?>', name='a.jpg'), 'html': upload(b'<html><script>1</script>', name='a.png'),
            'svg': upload(b'<svg xmlns="http://www.w3.org/2000/svg"><script>1</script></svg>', name='a.svg'),
            'zip': upload(b'PK\x03\x04' + b'\0' * 40, name='a.zip'), 'exe': upload(b'MZ\x90\x00' + b'\0' * 60, name='a.exe'),
            'gif': upload(b'GIF89a' + b'\0' * 40, name='a.gif'), 'empty': upload(b'', name='a.jpg'),
            'pdf': upload(b'%PDF-1.4\n%%EOF\n', name='a.pdf', content_type='application/pdf'),
        }
        for name, file in samples.items():
            with self.subTest(name=name):
                self.assertIn('JPG', self.error(file))

    def test_corrupt_images_are_rejected(self):
        good = make_image('JPEG', (200, 200))
        self.assertTrue(self.error(upload(good[:len(good) // 2])))
        self.assertTrue(self.error(upload(b'\xff\xd8\xff' + b'garbage' * 20)))

    def test_exif_and_trailing_data_are_stripped(self):
        item = self.prep(upload(make_image('JPEG', exif=True, trailing=b'<?php evil(); ?>')))[0]
        self.assertNotIn(b'SECRET-GPS-123', item.data)
        self.assertNotIn(b'<?php', item.data)
        self.assertEqual(dict(Image.open(io.BytesIO(item.data)).getexif()), {})

    def test_orientation_is_applied_and_big_images_are_shrunk(self):
        item = self.prep(upload(make_image('JPEG', (40, 20), orientation=6)))[0]
        self.assertEqual((item.width, item.height), (20, 40))
        big = self.prep(upload(make_image('PNG', (3000, 1000))))[0]
        self.assertEqual(max(big.width, big.height), 1920)

    def test_decompression_bombs_are_refused(self):
        with mock.patch('services.safe_images.MAX_PIXELS', 1000):
            self.assertIn('ابعاد', self.error(upload(make_image('PNG', (60, 60)))))

    def test_count_and_size_limits(self):
        self.assertIn('حداقل یک', self.error())
        pics = [upload(make_image(), name=f'{i}.jpg') for i in range(cheques.MAX_IMAGES + 1)]
        self.assertIn('حداکثر', self.error(*pics))
        self.assertEqual(len(self.prep(*pics[:cheques.MAX_IMAGES])), cheques.MAX_IMAGES)
        with mock.patch('orders.cheques.MAX_IMAGE_MB', 1):
            self.assertIn('مگابایت', self.error(upload(make_image() + b'\0' * (1024 * 1024 + 10))))

    def test_the_display_name_is_sanitized(self):
        item = self.prep(upload(make_image(), name='../../etc/pass<wd>.jpg'))[0]
        self.assertEqual(item.name, 'passwd.jpg')


# ------------------------------------------------------------------ جریان مشتری
class ChequeFlowBase(MediaIsolation, CheckoutTestBase):
    def cheque_order(self, user=None, **fields):
        user = user or self.user
        data = dict(user=user, first_name='علی', last_name='رضایی', phone='09120000021', address='تهران', total_price=500000,
                    payment_method='check')
        data.update(fields)
        return Order.objects.create(**data)

    def url(self, order):
        return reverse('orders:cheque_info', args=[order.id])

    def post_cheque(self, order, *, client=None, sayadi=VALID, images=None, **extra):
        data = {'sayadi_id': sayadi, 'images': images if images is not None else [upload(make_image())]}
        data.update(extra)
        return (client or self.client).post(self.url(order), data)


class FormAccessTests(ChequeFlowBase):
    def test_a_cheque_order_redirects_to_the_cheque_form_after_checkout(self):
        with mock.patch('holoo.receivers.send_order_to_holoo'):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.post_order({'address_id': self.address.pk, 'payment_method': 'check'})
        order = Order.objects.get(user=self.user)
        self.assertRedirects(response, self.url(order), fetch_redirect_response=False)

    def test_a_cash_order_still_goes_to_the_success_page(self):
        with mock.patch('holoo.receivers.send_order_to_holoo'):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.post_order({'address_id': self.address.pk, 'payment_method': 'cash'})
        order = Order.objects.get(user=self.user)
        self.assertRedirects(response, reverse('orders:order_success', args=[order.id]), fetch_redirect_response=False)

    def test_the_form_page_renders_the_fields_and_limits(self):
        order = self.cheque_order()
        response = self.client.get(self.url(order))
        self.assertContains(response, 'name="sayadi_id"')
        self.assertContains(response, 'name="images"')
        self.assertContains(response, 'accept="image/jpeg,image/png,image/webp"')
        self.assertContains(response, 'data-max-images="5"')
        self.assertNotContains(response, 'application/pdf')

    def test_only_the_owner_of_a_cheque_order_can_open_it(self):
        order = self.cheque_order()
        other = Client()
        other.force_login(make_approved_user('09120000099'))
        self.assertEqual(other.get(self.url(order)).status_code, 404)
        self.assertEqual(Client().get(self.url(order)).status_code, 302)                     # ناشناس ← ورود
        cash = self.cheque_order(payment_method='cash')
        self.assertEqual(self.client.get(self.url(cash)).status_code, 404)                   # سفارش غیرچکی
        self.assertEqual(self.client.get(reverse('orders:cheque_info', args=[999999])).status_code, 404)

    def test_the_vip_cheque_order_also_gets_the_form(self):
        order = self.cheque_order(payment_method='vip', settlement='cheque')
        self.assertEqual(self.client.get(self.url(order)).status_code, 200)


class CreateChequeTests(ChequeFlowBase):
    def test_a_valid_cheque_is_stored_with_clean_images_under_uuid_names(self):
        order = self.cheque_order()
        response = self.post_cheque(order, sayadi='۶۲۱۹۸۶۱۰۱۲۳۴۵۶۷۸', images=[upload(make_image('JPEG', exif=True), name='My Cheque.jpg'),
                                                                        upload(make_image('PNG'), name='back.png')],
                                    amount='1,200,000', due_date='1405/08/15', bank_name='ملی', holder_name='علی رضایی')
        self.assertRedirects(response, self.url(order), fetch_redirect_response=False)
        cheque = ChequePayment.objects.get()
        self.assertEqual((cheque.sayadi_id, int(cheque.amount), cheque.due_date, cheque.bank_name, cheque.holder_name, cheque.status),
                         (VALID, 1200000, date(2026, 11, 6), 'ملی', 'علی رضایی', 'pending_review'))
        self.assertEqual(cheque.images.count(), 2)
        files = stored_files(self.media_root)
        self.assertEqual(len(files), 2)
        for path in files:
            self.assertTrue(path.startswith(os.path.join(self.media_root, 'cheque_images')))
            self.assertRegex(os.path.basename(path), r'^[0-9a-f-]{36}\.(jpg|png)$')
            self.assertNotIn(b'SECRET-GPS-123', open(path, 'rb').read())
        self.assertNotIn('My Cheque', ' '.join(files))
        with self.assertRaises(ValueError):
            cheque.images.first().file.url                                 # نشانی عمومی وجود ندارد
        page = self.client.get(self.url(order))
        self.assertContains(page, VALID)
        self.assertContains(page, 'در انتظار بررسی')

    def test_an_order_can_have_several_cheques(self):
        order = self.cheque_order()
        self.post_cheque(order, sayadi=VALID)
        self.post_cheque(order, sayadi='6219861099999999')
        self.assertEqual(order.cheques.count(), 2)
        self.assertContains(self.client.get(self.url(order)), 'ثبت چک بعدی')

    def test_the_optional_fields_can_stay_empty(self):
        order = self.cheque_order()
        self.post_cheque(order)
        cheque = ChequePayment.objects.get()
        self.assertEqual((cheque.amount, cheque.due_date, cheque.bank_name, cheque.holder_name), (None, None, '', ''))

    def test_every_field_error_is_reported_at_once_and_nothing_is_saved(self):
        order = self.cheque_order()
        response = self.post_cheque(order, sayadi='123', images=[upload(b'%PDF-1.4\n%%EOF\n', name='a.pdf')], amount='abc', due_date='x')
        self.assertEqual(response.status_code, 400)
        for text in ('۱۶ رقم', 'JPG', 'مبلغ چک', 'قالب تاریخ'):
            self.assertContains(response, text, status_code=400)
        self.assertFalse(ChequePayment.objects.exists())
        self.assertEqual(stored_files(self.media_root), [])

    def test_at_least_one_image_is_required_and_at_most_five(self):
        order = self.cheque_order()
        self.assertContains(self.post_cheque(order, images=[]), 'حداقل یک تصویر', status_code=400)
        six = [upload(make_image(), name=f'{i}.jpg') for i in range(6)]
        self.assertContains(self.post_cheque(order, images=six), 'حداکثر 5 تصویر', status_code=400)
        self.assertFalse(ChequePayment.objects.exists())
        self.post_cheque(order, images=[upload(make_image(), name=f'{i}.jpg') for i in range(5)])
        self.assertEqual(ChequePayment.objects.get().images.count(), 5)

    def test_a_duplicate_sayadi_id_is_rejected(self):
        order = self.cheque_order()
        self.post_cheque(order)
        again = self.post_cheque(order)
        self.assertContains(again, 'قبلاً در سامانه ثبت شده', status_code=400)
        other_user = make_approved_user('09120000098')
        other_order = self.cheque_order(user=other_user, phone='09120000098')
        other_client = Client()
        other_client.force_login(other_user)
        self.assertContains(self.post_cheque(other_order, client=other_client), 'قبلاً در سامانه ثبت شده', status_code=400)
        self.assertEqual(ChequePayment.objects.count(), 1)

    def test_a_rejected_cheque_or_a_canceled_order_frees_the_sayadi_id(self):
        order = self.cheque_order()
        self.post_cheque(order)
        other_user = make_approved_user('09120000097')
        other_order = self.cheque_order(user=other_user, phone='09120000097')
        other_client = Client()
        other_client.force_login(other_user)
        ChequePayment.objects.update(status='rejected')
        self.assertEqual(self.post_cheque(other_order, client=other_client).status_code, 302)
        Order.objects.filter(pk=other_order.pk).update(status='canceled')
        ChequePayment.objects.filter(order=order).update(status='pending_review')
        third = self.cheque_order(phone='09120000021')
        self.assertContains(self.post_cheque(third), 'قبلاً در سامانه ثبت شده', status_code=400)       # چک سفارش اول هنوز فعال است

    def test_closed_orders_do_not_accept_cheques(self):
        for fields in ({'status': 'canceled'}, {'status': 'rejected_stock'}):
            order = self.cheque_order(**fields)
            response = self.post_cheque(order)
            self.assertEqual(response.status_code, 409)
            self.assertFalse(order.cheques.exists())
        approved = self.cheque_order()
        Order.objects.filter(pk=approved.pk).update(approved_at='2026-10-01T00:00:00Z')
        self.assertEqual(self.post_cheque(approved).status_code, 409)

    def test_the_per_order_cheque_cap(self):
        order = self.cheque_order()
        with mock.patch('orders.cheques.MAX_CHEQUES_PER_ORDER', 1):
            self.post_cheque(order)
            response = self.post_cheque(order, sayadi='6219861099999999')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(order.cheques.count(), 1)

    def test_a_failure_midway_leaves_no_cheque_and_no_files(self):
        order = self.cheque_order()
        real = ChequeImage.save
        calls = {'n': 0}

        def flaky(instance, *args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 2:
                raise RuntimeError('disk full')
            return real(instance, *args, **kwargs)

        with mock.patch.object(ChequeImage, 'save', flaky):
            with self.assertRaises(RuntimeError):
                self.post_cheque(order, images=[upload(make_image(), name='1.jpg'), upload(make_image('PNG'), name='2.png')])
        self.assertFalse(ChequePayment.objects.exists())
        self.assertEqual(stored_files(self.media_root), [])

    def test_the_rate_limit_is_checked_before_the_expensive_decode(self):
        order = self.cheque_order()
        with mock.patch('orders.cheques.RATE_LIMIT', 1):
            self.post_cheque(order)
            with mock.patch('orders.cheques.clean_image') as decode:
                response = self.post_cheque(order, sayadi='6219861099999999')
        self.assertEqual(response.status_code, 429)
        decode.assert_not_called()

    def test_deleting_the_order_removes_its_files(self):
        order = self.cheque_order()
        self.post_cheque(order)
        self.assertEqual(len(stored_files(self.media_root)), 1)
        with self.captureOnCommitCallbacks(execute=True):                   # django_cleanup بعد از commit فایل را پاک می‌کند
            order.delete()
        self.assertEqual(stored_files(self.media_root), [])

    def test_tests_never_touch_the_real_media_folder(self):
        self.assertEqual(os.path.normcase(str(settings.MEDIA_ROOT)), os.path.normcase(self.media_root))
        self.assertNotEqual(os.path.normcase(str(settings.MEDIA_ROOT)), os.path.normcase(str(settings.BASE_DIR / 'media')))


# ------------------------------------------------------------------ دسترسی به تصویر
class ImageAccessTests(ChequeFlowBase):
    def setUp(self):
        super().setUp()
        self.order = self.cheque_order()
        self.post_cheque(self.order)
        self.image = ChequeImage.objects.get()
        self.url_image = reverse('orders:cheque_image', args=[self.image.public_id])

    def test_the_owner_gets_the_image_with_hardened_headers(self):
        response = self.client.get(self.url_image)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/jpeg')
        self.assertTrue(response['Content-Disposition'].startswith('inline'))
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertIn('sandbox', response['Content-Security-Policy'])
        self.assertIn('private', response['Cache-Control'])
        Image.open(io.BytesIO(b''.join(response.streaming_content))).verify()

    def test_everyone_else_gets_a_404_or_a_login_redirect(self):
        stranger = Client()
        stranger.force_login(make_approved_user('09120000096'))
        self.assertEqual(stranger.get(self.url_image).status_code, 404)
        self.assertEqual(Client().get(self.url_image).status_code, 302)
        import uuid
        self.assertEqual(self.client.get(reverse('orders:cheque_image', args=[uuid.uuid4()])).status_code, 404)

    def test_the_admin_can_view_it_and_plain_staff_cannot(self):
        admin = Client()
        admin.force_login(make_approved_user('09120000095', is_staff=True, is_superuser=True))
        url = reverse('admin:orders_chequepayment_image', args=[self.image.public_id])
        self.assertEqual(admin.get(url).status_code, 200)
        staff = Client()
        staff.force_login(make_approved_user('09120000094', is_staff=True))
        self.assertEqual(staff.get(url).status_code, 403)
        self.assertEqual(Client().get(url).status_code, 302)

    def test_the_admin_pages_show_the_cheques_read_only(self):
        admin = Client()
        admin.force_login(make_approved_user('09120000093', is_staff=True, is_superuser=True))
        order_page = admin.get(reverse('admin:orders_order_change', args=[self.order.id]))
        self.assertContains(order_page, VALID)
        self.assertContains(order_page, reverse('admin:orders_chequepayment_image', args=[self.image.public_id]))
        listing = admin.get(reverse('admin:orders_chequepayment_changelist'))
        self.assertContains(listing, VALID)
        self.assertEqual(admin.get(reverse('admin:orders_chequepayment_add')).status_code, 403)
        cheque = ChequePayment.objects.get()
        self.assertEqual(admin.post(reverse('admin:orders_chequepayment_delete', args=[cheque.pk]), {'post': 'yes'}).status_code, 403)


# ------------------------------------------------------------------ صفحه‌های سفارش
class OrderPagesTests(ChequeFlowBase):
    def test_the_success_page_offers_the_cheque_form_only_for_cheque_orders(self):
        cheque_order = self.cheque_order()
        self.assertContains(self.client.get(reverse('orders:order_success', args=[cheque_order.id])), self.url(cheque_order))
        cash = self.cheque_order(payment_method='cash')
        self.assertNotContains(self.client.get(reverse('orders:order_success', args=[cash.id])), 'ثبت اطلاعات چک')

    def test_the_order_detail_shows_the_cheque_box_and_its_state(self):
        order = self.cheque_order()
        detail = reverse('orders:order_detail_full', args=[order.id])
        self.assertContains(self.client.get(detail), 'هنوز اطلاعات چک این سفارش ثبت نشده است')
        self.post_cheque(order)
        page = self.client.get(detail)
        self.assertContains(page, VALID)
        self.assertContains(page, 'در انتظار بررسی')
        Order.objects.filter(pk=order.pk).update(approved_at='2026-10-01T00:00:00Z')
        self.assertNotContains(self.client.get(detail), 'ثبت چک بعدی')

    def test_a_non_cheque_order_detail_has_no_cheque_box(self):
        cash = self.cheque_order(payment_method='cash')
        self.assertNotContains(self.client.get(reverse('orders:order_detail_full', args=[cash.id])), 'cheque-box')
