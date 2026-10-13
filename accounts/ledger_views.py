"""
صفحه‌ی «معین حساب» پنل مشتری و خروجی CSV آن. منطق در accounts/ledger.py است؛ این‌جا فقط ورودی وب (بازه‌ی تاریخ، صفحه) و
قالب. کد حساب هرگز از درخواست خوانده نمی‌شود: همیشه حساب خودِ request.user.
"""

from datetime import date

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.views.generic import TemplateView, View

from . import ledger

PRESETS = (('30', '۳۰ روز اخیر'), ('90', '۳ ماه اخیر'), ('year', 'امسال'), ('all', 'همه'))


def _filters(request):
    """ (از، تا، مقدار نمایشیِ فیلدها، پیام خطا) از روی GET؛ پیش‌فرض: همه """
    preset = request.GET.get('range', '')
    text_from, text_to = request.GET.get('from', '').strip(), request.GET.get('to', '').strip()
    error = ''
    if preset in dict(PRESETS):
        date_from, date_to = ledger.preset_range(preset, date.today())
        return date_from, date_to, {'range': preset, 'from': '', 'to': ''}, error
    date_from = ledger.parse_jalali(text_from) if text_from else None
    date_to = ledger.parse_jalali(text_to) if text_to else None
    if (text_from and date_from is None) or (text_to and date_to is None):
        error = 'تاریخ را به شکل ۱۴۰۵/۰۷/۰۱ وارد کنید.'
        date_from = date_to = None
    elif date_from and date_to and date_from > date_to:
        error = 'تاریخ «از» باید قبل از «تا» باشد.'
        date_from = date_to = None
    values = {'range': 'custom' if (text_from or text_to) else 'all', 'from': text_from, 'to': text_to}
    return date_from, date_to, values, error


class LedgerView(LoginRequiredMixin, TemplateView):
    template_name = 'accounts/ledger.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        request = self.request
        refresh = request.GET.get('refresh') == '1'
        result, _fresh = ledger.load_entries(request.user, refresh=refresh)
        status = result.get('status')

        date_from, date_to, values, error = _filters(request)
        context.update({'active_nav': 'ledger', 'status': status, 'presets': PRESETS,
                        'filter_values': values, 'filter_error': error})
        if status != 'ok':
            return context

        all_rows = ledger.build_rows(result.get('entries', []))
        selected, opening = ledger.apply_range(all_rows, date_from, date_to)
        summary = ledger.summarize(all_rows, selected, opening)
        current_amount, current_label = ledger.balance_label(summary['current_balance'])
        closing_amount, closing_label = ledger.balance_label(summary['closing_balance'])
        opening_amount, opening_label = ledger.balance_label(summary['opening_balance'])

        page_obj = Paginator(list(reversed(selected)), ledger.PAGE_SIZE).get_page(request.GET.get('page'))
        for row in page_obj.object_list:
            row['balance_amount'], row['balance_label'] = ledger.balance_label(row['balance'])

        query = request.GET.copy()
        query.pop('page', None)
        query.pop('refresh', None)
        context.update({
            'summary': summary, 'has_entries': bool(all_rows),
            'current_amount': current_amount, 'current_label': current_label,
            'closing_amount': closing_amount, 'closing_label': closing_label,
            'opening_amount': opening_amount, 'opening_label': opening_label,
            'page_obj': page_obj, 'rows': page_obj.object_list,
            'fetched_at': result.get('fetched_at'),
            'query_string': query.urlencode(),
        })
        return context


class LedgerExportView(LoginRequiredMixin, View):
    def get(self, request):
        result, _fresh = ledger.load_entries(request.user)
        if result.get('status') != 'ok':
            return HttpResponse('معین حساب در دسترس نیست.', status=503, content_type='text/plain; charset=utf-8')
        date_from, date_to, _values, error = _filters(request)
        if error:
            date_from = date_to = None
        selected, _opening = ledger.apply_range(ledger.build_rows(result.get('entries', [])), date_from, date_to)
        response = HttpResponse(ledger.to_csv(list(reversed(selected))), content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="moein-hesab-{date.today():%Y%m%d}.csv"'
        response['Cache-Control'] = 'private, no-store'
        return response
