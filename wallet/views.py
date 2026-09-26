"""
ویوهای کیف پول: داشبورد، جریان شارژ آزمایشی (Mock Top-up)، و درخواست برداشت/تسویه.

جریان شارژ عمداً همان الگوی payments/views.py را تکرار می‌کند (start -> mock gateway ->
callback با select_for_update + idempotent) اما کاملاً مستقل از Order/payments.Transaction -
نگاه کنید توضیح بالای wallet/services.py برای چرایی این استقلال.

هرگز مستقیم wallet.balance/reserved_balance را اینجا تغییر ندهید؛ همه چیز باید از
wallet/services.py رد شود (credit_wallet/reserve_withdrawal/approve_withdrawal/mark_withdrawal_paid/...).
"""

import secrets
from decimal import Decimal, InvalidOperation

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.db import transaction as db_transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic import TemplateView

from . import services
from .forms import WithdrawalRequestForm
from .models import Wallet, WalletTransaction, WalletTopupRequest

MIN_TOPUP_AMOUNT = Decimal('50000')
PRESET_TOPUP_AMOUNTS = (50000, 100000, 200000, 500000)


def _get_wallet(user):
    """ کیف پول کاربر؛ اگر هنوز ندارد همین‌جا با موجودی صفر ساخته می‌شود (لازی، بدون نیاز به سیگنال ثبت‌نام) """
    wallet, _ = Wallet.objects.get_or_create(user=user)
    return wallet


class WalletDashboardView(LoginRequiredMixin, TemplateView):
    """ موجودی کل/بلوکه/قابل‌استفاده + تاریخچه‌ی تراکنش‌ها با فیلتر نوع و صفحه‌بندی """
    template_name = 'wallet/wallet_dashboard.html'
    PAGE_SIZE = 10

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        wallet = _get_wallet(self.request.user)

        kind_filter = self.request.GET.get('kind', 'all')
        transactions = wallet.transactions.all()
        if kind_filter == 'deposit':
            transactions = transactions.filter(amount__gt=0)
        elif kind_filter == 'withdraw':
            transactions = transactions.filter(amount__lt=0)

        paginator = Paginator(transactions, self.PAGE_SIZE)
        page_obj = paginator.get_page(self.request.GET.get('page'))

        context.update({
            'active_nav': 'wallet',
            'wallet': wallet,
            'page_obj': page_obj,
            'transactions': page_obj.object_list,
            'kind_filter': kind_filter,
        })
        return context


class WalletTopupView(LoginRequiredMixin, TemplateView):
    """ فرم انتخاب مبلغ شارژ؛ با ثبت موفق یک WalletTopupRequest به درگاه Mock هدایت می‌شود """
    template_name = 'wallet/topup_form.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({
            'active_nav': 'wallet',
            'wallet': _get_wallet(self.request.user),
            'preset_amounts': PRESET_TOPUP_AMOUNTS,
            'min_amount': MIN_TOPUP_AMOUNT,
        })
        return context

    def post(self, request, *args, **kwargs):
        wallet = _get_wallet(request.user)
        raw_amount = (request.POST.get('amount') or '').replace(',', '').strip()
        try:
            amount = Decimal(raw_amount)
        except (InvalidOperation, ValueError):
            amount = None

        if amount is None or amount < MIN_TOPUP_AMOUNT:
            context = self.get_context_data(**kwargs)
            context['error'] = f'مبلغ واردشده نامعتبر است؛ حداقل مبلغ افزایش موجودی {int(MIN_TOPUP_AMOUNT):,} تومان است.'
            context['submitted_amount'] = raw_amount
            return render(request, self.template_name, context)

        # اتوریتی غیرقابل‌حدس (secrets نه random) - دقیقاً همان توجیه payments/views.py:PaymentStartView
        authority = f'WLT{secrets.token_hex(16).upper()}'
        WalletTopupRequest.objects.create(wallet=wallet, amount=amount, authority=authority)
        return redirect('wallet:mock_gateway', authority=authority)


class WalletMockGatewayView(LoginRequiredMixin, View):
    """ صفحه‌ی میانی شبیه‌سازِ درگاه بانکی (مثل payments.MockGatewayView) """
    template_name = 'wallet/mock_gateway.html'

    def get(self, request, authority, *args, **kwargs):
        topup = get_object_or_404(
            WalletTopupRequest, authority=authority, status=WalletTopupRequest.STATUS_PENDING, wallet__user=request.user,
        )
        return render(request, self.template_name, {'topup': topup})


class WalletTopupCallbackView(LoginRequiredMixin, View):
    """
    بازگشت از درگاه Mock. دقیقاً هم‌الگوی payments.PaymentCallbackView: select_for_update +
    چک idempotent (اگر قبلاً پردازش شده، فقط نتیجه دوباره نشان داده می‌شود، بدون credit_wallet
    دوباره) تا رفرش صفحه‌ی نتیجه دوبار شارژ نکند.
    """

    def get(self, request, *args, **kwargs):
        authority = request.GET.get('authority')
        if not authority:
            raise Http404('authority missing')

        topup = get_object_or_404(WalletTopupRequest, authority=authority, wallet__user=request.user)
        succeeded = request.GET.get('status') == 'OK'

        with db_transaction.atomic():
            locked = (
                WalletTopupRequest.objects.select_for_update().select_related('wallet').get(pk=topup.pk)
            )
            if locked.status != WalletTopupRequest.STATUS_PENDING:
                return render(request, 'wallet/topup_result.html', {
                    'topup': locked, 'success': locked.status == WalletTopupRequest.STATUS_SUCCESS,
                    'wallet': locked.wallet,
                })

            if succeeded:
                txn = services.credit_wallet(
                    locked.wallet, locked.amount, WalletTransaction.KIND_TOPUP,
                    reference_type='wallet_topup_request', reference_id=locked.pk, created_by=request.user,
                )
                locked.status = WalletTopupRequest.STATUS_SUCCESS
                locked.ref_id = str(secrets.randbelow(90000000) + 10000000)
                locked.transaction = txn
                locked.save(update_fields=['status', 'ref_id', 'transaction', 'updated_at'])
                # credit_wallet روی نمونه‌ی دیگری از Wallet (نه locked.wallet) قفل و ذخیره می‌کند؛
                # برای این‌که مبلغ تازه در صفحه‌ی نتیجه درست نشان داده شود، همان نمونه‌ی به‌روز را می‌گیریم
                wallet = txn.wallet
            else:
                locked.status = WalletTopupRequest.STATUS_FAILED
                locked.save(update_fields=['status', 'updated_at'])
                wallet = locked.wallet

        return render(request, 'wallet/topup_result.html', {'topup': locked, 'success': succeeded, 'wallet': wallet})


class WalletWithdrawView(LoginRequiredMixin, TemplateView):
    """
    فرم درخواست برداشت + سوابق درخواست‌های قبلی کاربر. تأیید/رد فقط در ادمین انجام می‌شود
    (wallet/admin.py)؛ این ویو فقط reserve_withdrawal را صدا می‌زند - هرگز balance/reserved_balance
    را مستقیم دست نمی‌زند.
    """
    template_name = 'wallet/withdraw_form.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        wallet = _get_wallet(self.request.user)
        context.update({
            'active_nav': 'wallet',
            'wallet': wallet,
            'form': kwargs.get('form') or WithdrawalRequestForm(wallet=wallet),
            'withdrawal_requests': wallet.withdrawal_requests.all(),
        })
        return context

    def post(self, request, *args, **kwargs):
        wallet = _get_wallet(request.user)
        form = WithdrawalRequestForm(request.POST, wallet=wallet)
        if not form.is_valid():
            return render(request, self.template_name, self.get_context_data(form=form))

        try:
            services.reserve_withdrawal(
                wallet, form.cleaned_data['amount'],
                account_holder=form.cleaned_data['account_holder'],
                card_number=form.cleaned_data['card_number'],
                iban=form.cleaned_data['iban'],
            )
        except services.InsufficientBalanceError:
            # مسیر نادر: موجودی بین لحظه‌ی چک فرم و لحظه‌ی قفل واقعی سرویس تغییر کرده
            # (مثلاً یک تب دیگر هم‌زمان درخواست زده)؛ پیام دوستانه به‌جای خطای ۵۰۰
            form.add_error('amount', 'موجودی قابل‌استفاده کافی نیست.')
            return render(request, self.template_name, self.get_context_data(form=form))

        return redirect('wallet:withdraw')
