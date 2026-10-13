"""
«معین حساب» مشتری: پردازش گردش (مانده‌ی تجمعی، بازه‌ی تاریخ)، ترجمه‌ی پاسخ کاردکس هلو، صفحه‌ی پنل (وضعیت‌ها، امنیت مالکیت،
فیلتر، CSV) و منوی پنل. هیچ تماسی با هلو واقعی نمی‌رود؛ تأمین‌کننده با mock جایگزین می‌شود.
"""
from datetime import date
from decimal import Decimal
from unittest import mock

import jdatetime
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from accounts import ledger
from accounts.models import CustomUser
from holoo import ledger as holoo_ledger


def entry(ref, day, bed=0, bes=0, comment='فاکتور فروش 1'):
    return {'ref': str(ref), 'date': day, 'bed': Decimal(bed), 'bes': Decimal(bes), 'comment': comment}


ENTRIES = [
    entry(30, date(2026, 8, 20), bes=1_000, comment='دریافت حواله واریزی به بانک ملی'),
    entry(1, date(2026, 3, 21), bed=5_000, comment='سند افتتاحیه'),
    entry(20, date(2026, 8, 10), bed=2_500, comment='فاکتور فروش 34369'),
    entry(21, date(2026, 8, 10), bes=500, comment='برگشت از فروش 12'),
]


class ProcessingTests(SimpleTestCase):
    def test_rows_are_chronological_with_running_balance(self):
        rows = ledger.build_rows(ENTRIES)
        self.assertEqual([r['ref'] for r in rows], ['1', '20', '21', '30'])
        self.assertEqual([r['balance'] for r in rows], [Decimal(5000), Decimal(7500), Decimal(7000), Decimal(6000)])
        self.assertEqual(rows[0]['jalali'], jdatetime.date.fromgregorian(date=date(2026, 3, 21)).strftime('%Y/%m/%d'))

    def test_kinds(self):
        kinds = {r['ref']: r['kind'] for r in ledger.build_rows(ENTRIES)}
        self.assertEqual(kinds, {'1': 'opening', '20': 'invoice', '21': 'return', '30': 'payment'})
        self.assertEqual(ledger.classify('فاكتور فروش 5')[0], 'invoice')        # ك عربی

    def test_range_uses_the_balance_before_it_as_opening(self):
        rows = ledger.build_rows(ENTRIES)
        selected, opening = ledger.apply_range(rows, date(2026, 8, 1), date(2026, 8, 15))
        self.assertEqual([r['ref'] for r in selected], ['20', '21'])
        self.assertEqual(opening, Decimal(5000))
        summary = ledger.summarize(rows, selected, opening)
        self.assertEqual((summary['total_bed'], summary['total_bes'], summary['closing_balance'], summary['current_balance']),
                         (Decimal(2500), Decimal(500), Decimal(7000), Decimal(6000)))

    def test_balance_label(self):
        self.assertEqual(ledger.balance_label(Decimal(10)), (Decimal(10), 'بدهکار'))
        self.assertEqual(ledger.balance_label(Decimal(-10)), (Decimal(10), 'بستانکار'))
        self.assertEqual(ledger.balance_label(Decimal(0)), (Decimal(0), 'تسویه'))

    def test_jalali_parsing(self):
        self.assertEqual(ledger.parse_jalali('1405/01/01'), jdatetime.date(1405, 1, 1).togregorian())
        self.assertEqual(ledger.parse_jalali('۱۴۰۵-۷-۱'), jdatetime.date(1405, 7, 1).togregorian())
        self.assertIsNone(ledger.parse_jalali('1405/13/01'))
        self.assertIsNone(ledger.parse_jalali('abc'))
        self.assertIsNone(ledger.parse_jalali(''))

    def test_csv_is_safe_and_ordered_as_given(self):
        rows = ledger.build_rows([entry(1, date(2026, 8, 1), bed=100, comment='=HYPERLINK("x")')])
        text = ledger.to_csv(rows)
        self.assertTrue(text.startswith('﻿'))
        self.assertIn("'=HYPERLINK", text)


class HolooKardexParsingTests(SimpleTestCase):
    def test_parse_rows_and_single_object(self):
        data = {'report': [
            {'Code': '23883', 'Date': '2026/08/27', 'Bed': 1.776E7, 'Bes': 0.0, 'Comment': ' فاکتور فروش 34369'},
            {'Code': '22688', 'Date': '2026/08/18', 'Bed': 0.0, 'Bes': 3554000.0, 'Comment': 'دریافت حواله'},
            {'Code': '1', 'Date': 'bad', 'Bed': 1, 'Bes': 0, 'Comment': 'x'},
        ]}
        entries = holoo_ledger.parse_kardex(data)
        self.assertEqual(len(entries), 2)
        self.assertEqual((entries[0]['date'], entries[0]['bed'], entries[0]['comment']),
                         (date(2026, 8, 27), Decimal(17760000), 'فاکتور فروش 34369'))
        single = holoo_ledger.parse_kardex({'report': {'Code': '5', 'Date': '2026/01/02', 'Bed': 10, 'Bes': 0, 'Comment': 'y'}})
        self.assertEqual(len(single), 1)
        self.assertEqual(holoo_ledger.parse_kardex({}), [])

    def test_provider_statuses(self):
        user = mock.Mock(holoo_bed_sarfasl='')
        self.assertEqual(holoo_ledger.fetch_customer_ledger(user)['status'], 'not_linked')
        user.holoo_bed_sarfasl = '1033821'
        with mock.patch('holoo.ledger.HolooClient') as client:
            client.return_value.config.timeout = 30
            client.return_value.get_json.return_value = None
            self.assertEqual(holoo_ledger.fetch_customer_ledger(user)['status'], 'unavailable')
            client.return_value.get_json.return_value = {'report': [
                {'Code': '1', 'Date': '2026/01/02', 'Bed': 10, 'Bes': 0, 'Comment': 'y'}]}
            result = holoo_ledger.fetch_customer_ledger(user)
            self.assertEqual((result['status'], len(result['entries'])), ('ok', 1))
            args, kwargs = client.return_value.get_json.call_args
            self.assertEqual(kwargs['params'], {'code': '1033821', 'fa': 'true'})


class LedgerPageTests(TestCase):
    def setUp(self):
        from accounts.testing import make_approved_user
        self.user = make_approved_user('09125550101')
        CustomUser.objects.filter(pk=self.user.pk).update(holoo_bed_sarfasl='1033821')
        self.user.refresh_from_db()
        self.client.force_login(self.user)
        self.url = reverse('accounts:ledger')
        cache.clear()
        provider = mock.patch.object(ledger, '_provider', lambda user: {'status': 'ok', 'entries': list(ENTRIES)})
        provider.start()
        self.addCleanup(provider.stop)

    def test_requires_login(self):
        self.client.logout()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get(reverse('accounts:ledger_export')).status_code, 302)

    def test_page_shows_balance_summary_and_rows_newest_first(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-testid="ledger-table"')
        self.assertContains(response, '6,000')                                   # مانده‌ی فعلی
        self.assertContains(response, 'بدهکار')
        html = response.content.decode()
        self.assertLess(html.index('دریافت حواله واریزی'), html.index('سند افتتاحیه'))      # جدیدترین اول
        self.assertEqual(response.context['summary']['current_balance'], Decimal(6000))

    def test_range_filter(self):
        response = self.client.get(self.url, {'from': '1405/05/01', 'to': '1405/05/31'})     # ≈ آگوست ۲۰۲۶
        self.assertEqual(response.status_code, 200)
        refs = [r['ref'] for r in response.context['rows']]
        self.assertEqual(sorted(refs), ['20', '21', '30'])

    def test_bad_date_shows_an_error_and_falls_back_to_all(self):
        response = self.client.get(self.url, {'from': 'zzz'})
        self.assertContains(response, 'تاریخ را به شکل')
        self.assertEqual(len(response.context['rows']), 4)

    def test_other_users_account_code_in_the_query_is_ignored(self):
        calls = []

        def provider(user):
            calls.append(user.pk)
            return {'status': 'ok', 'entries': []}
        with mock.patch.object(ledger, '_provider', provider):
            self.client.get(self.url, {'code': '9999999', 'user': '1', 'sarfasl': '9999999', 'refresh': '1'})
        self.assertEqual(calls, [self.user.pk])

    def test_not_linked_and_unavailable_states(self):
        with mock.patch.object(ledger, '_provider', lambda user: {'status': 'not_linked', 'entries': []}):
            cache.clear()
            self.assertContains(self.client.get(self.url), 'data-testid="ledger-not-linked"')
        with mock.patch.object(ledger, '_provider', lambda user: {'status': 'unavailable', 'entries': []}):
            cache.clear()
            response = self.client.get(self.url)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'data-testid="ledger-unavailable"')

    def test_provider_failure_does_not_break_the_page(self):
        def boom(user):
            raise RuntimeError('holoo down')
        with mock.patch.object(ledger, '_provider', boom):
            cache.clear()
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-testid="ledger-unavailable"')

    def test_result_is_cached_and_refresh_has_a_cooldown(self):
        calls = []

        def provider(user):
            calls.append(1)
            return {'status': 'ok', 'entries': list(ENTRIES)}
        with mock.patch.object(ledger, '_provider', provider):
            cache.clear()
            self.client.get(self.url)
            self.client.get(self.url)
            self.assertEqual(len(calls), 1)
            self.client.get(self.url, {'refresh': '1'})
            self.assertEqual(len(calls), 2)
            self.client.get(self.url, {'refresh': '1'})      # داخل cooldown: دوباره نمی‌خواند
            self.assertEqual(len(calls), 2)

    def test_csv_export_follows_the_filter(self):
        response = self.client.get(reverse('accounts:ledger_export'), {'from': '1405/05/01', 'to': '1405/05/31'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('text/csv', response['Content-Type'])
        self.assertIn('attachment', response['Content-Disposition'])
        body = response.content.decode('utf-8')
        self.assertIn('برگشت از فروش', body)
        self.assertNotIn('سند افتتاحیه', body)

    def test_nav_item_is_under_orders(self):
        response = self.client.get(self.url)
        html = response.content.decode()
        self.assertIn('معین حساب', html)
        self.assertLess(html.index('سفارش‌های من'), html.index(reverse('accounts:ledger')))
