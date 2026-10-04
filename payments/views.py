import logging
import secrets
from decimal import Decimal, InvalidOperation
from django.contrib import messages
from django.db import transaction as db_transaction
from django.http import Http404
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.views import View
from django.contrib.auth.mixins import LoginRequiredMixin

from products.pricing import CHECK as PRICING_CHECK
from orders.models import Order
from orders.stock_hooks import ensure_order_hold
from products.stock import InsufficientStock
from wallet.models import Wallet
from wallet.services import InsufficientBalanceError
from . import checkout
from .checkout import InvalidWalletAmountError
from .models import Transaction
from .signals import payment_succeeded

logger = logging.getLogger(__name__)


def _parse_wallet_amount(text):
    """ سهم کیف‌پول ارسالیِ فرم ← Decimal، وگرنه صفر (یعنی «کیف‌پول استفاده نشود»، نه خطا) """
    text = (text or '').strip()
    if not text.isascii() or not text.isdigit():
        return Decimal('0')
    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal('0')


class PaymentStartView(LoginRequiredMixin, View):
    """
    انتخاب روش پرداخت و شروع فرآیند (Wallet Phase 4: کیف‌پول/درگاه/ترکیبی).

    GET: صفحه‌ی انتخاب («چقدر از کیف‌پول استفاده شود») را نشان می‌دهد.
    POST: تصمیم واقعی را به payments.checkout.start_order_payment می‌سپارد؛ خودِ این ویو فقط
    ورودی کاربر را می‌خواند و بر اساس خروجی (نتیجه‌ی نهایی یا هدایت به درگاه) ریدایرکت می‌کند.
    """
    template_name = 'payments/choose_method.html'

    def _get_order(self, request, order_id):
        """
        بازمی‌گرداند: (order, error_redirect). اگر مجاز است error_redirect=None.
        سفارش چکی از هر سفارش غیرقابل‌پرداختِ دیگر (پرداخت‌شده/لغوشده) تفکیک می‌شود چون باید
        با پیام مشخص به صفحه‌ی جزئیات سفارش برگردد، نه صرفاً به تاریخچه‌ی خام.
        """
        order = get_object_or_404(Order, id=order_id, user=request.user)
        if order.payment_method == PRICING_CHECK:
            return None, redirect(
                f"{reverse('orders:order_detail_full', args=[order.id])}?payment_blocked_reason=cheque"
            )
        if not order.can_pay:
            return None, redirect('orders:order_history')
        return order, None

    def get(self, request, order_id, *args, **kwargs):
        order, error_redirect = self._get_order(request, order_id)
        if error_redirect is not None:
            return error_redirect

        # تراکنشِ در انتظارِ قبلی (مثلاً کاربر وسط راه برگشته و دوباره «پرداخت» زده) دوباره
        # استفاده می‌شود؛ وگرنه با هر کلیک یک ردیف pending بی‌استفاده در دیتابیس تلنبار می‌شد
        pending = Transaction.objects.filter(order=order, user=request.user, status='pending').first()
        if pending:
            return redirect('payments:mock_gateway', authority=pending.authority)

        wallet, _ = Wallet.objects.get_or_create(user=request.user)
        # پیش‌فرض هوشمند کادر سهم کیف‌پول: اگر موجودی کمتر از سفارش است، حداکثر موجودی
        # قابل‌استفاده (کاربر فقط در صورت نیاز کم می‌کند)؛ اگر بیشتر است، کل مبلغ سفارش
        default_wallet_amount = min(order.total_price, wallet.available_balance)
        return render(request, self.template_name, {
            'order': order, 'wallet': wallet, 'default_wallet_amount': default_wallet_amount,
        })

    def post(self, request, order_id, *args, **kwargs):
        order, error_redirect = self._get_order(request, order_id)
        if error_redirect is not None:
            return error_redirect

        # پیش از هر پرداخت: رزرو موجودی تازه می‌شود (مهلت ۲۰ دقیقه از همین لحظه) یا اگر منقضی شده و کالا دیگر نیست،
        # قبل از رفتن به درگاه/کسر کیف‌پول جلوی پرداخت گرفته می‌شود
        try:
            ensure_order_hold(order)
        except InsufficientStock:
            order.status = 'canceled'
            order.cancel_reason = 'نبود موجودی پس از پایان مهلت رزرو'
            order.save()
            messages.error(request, 'مهلت رزرو سفارش تمام شده و موجودی برخی کالاها دیگر کافی نیست؛ سفارش لغو شد. '
                                    'لطفاً دوباره سفارش دهید.')
            return redirect('orders:order_detail_full', order.id)

        wallet_amount_requested = _parse_wallet_amount(request.POST.get('wallet_amount'))
        try:
            txn, redirect_kind = checkout.start_order_payment(order, request.user, wallet_amount_requested)
        except (InvalidWalletAmountError, InsufficientBalanceError) as e:
            wallet, _ = Wallet.objects.get_or_create(user=request.user)
            return render(
                request, self.template_name, {'order': order, 'wallet': wallet, 'error': str(e)}, status=400,
            )

        if redirect_kind == 'result':
            return render(request, 'payments/result.html', {'transaction': txn, 'success': True})
        return redirect('payments:mock_gateway', authority=txn.authority)


class MockGatewayView(LoginRequiredMixin, View):
    """ صفحه شبیه‌ساز درگاه پرداخت (مثل صفحه زرین‌پال) """
    template_name = 'payments/mock_gateway.html'

    def get(self, request, authority, *args, **kwargs):
        # تراکنش باید هم در انتظار باشد و هم متعلق به خودِ کاربر (قبلاً هر کسی با داشتن
        # authority می‌توانست صفحه‌ی درگاه سفارش دیگری را باز کند)
        txn = get_object_or_404(Transaction, authority=authority, status='pending', user=request.user)
        return render(request, self.template_name, {'transaction': txn})


class PaymentCallbackView(View):
    """
    بازگشت از درگاه پرداخت.

    عمداً LoginRequiredMixin ندارد: اگر سشن کاربر در فاصله‌ی رفتن به بانک و برگشتن منقضی شود،
    اجبار به لاگین باعث می‌شد کل نتیجه‌ی پرداخت گم شود. محافظت‌ها به‌جای آن:
      - authority یک توکن غیرقابل‌حدس است و نقش کلید دسترسی را دارد
      - اگر کاربرِ لاگین‌کرده صاحب تراکنش نباشد، ۴۰۴ می‌گیرد
      - کل تغییر وضعیت داخل یک تراکنش دیتابیس با select_for_update انجام می‌شود
      - عملیات جانبی (پیامک، ثبت سند در هلو) فقط یک‌بار و فقط پس از commit اجرا می‌شوند

    ⚠️ قبل از اتصال درگاه واقعی: مقدار Status از کوئری‌استرینگ به‌تنهایی هرگز نباید ملاک
    موفقیت باشد. متد _verify_payment باید با فراخوانی endpoint تاییدِ خودِ بانک (Verify)
    و تطبیق مبلغ بازگشتی جایگزین شود؛ در غیر این صورت هر کسی با زدن ?Status=OK پرداخت
    جعلی ثبت می‌کند. ساختار این ویو طوری نوشته شده که فقط همین یک متد عوض شود.
    """

    def _verify_payment(self, request, txn):
        """
        تایید پرداخت نزد درگاه. خروجی: (موفق؟، شماره پیگیری بانک یا None)
        نسخه‌ی فعلی شبیه‌سازی‌شده است (درگاه Mock).
        """
        if request.GET.get('Status') != 'OK':
            return False, None
        return True, str(secrets.randbelow(90000000) + 10000000)

    def get(self, request, *args, **kwargs):
        authority = request.GET.get('Authority')
        if not authority:
            raise Http404("Authority missing")

        txn = get_object_or_404(Transaction, authority=authority)

        # اگر کاربری لاگین است، باید صاحب همین تراکنش باشد
        if request.user.is_authenticated and txn.user_id != request.user.id:
            raise Http404("Transaction does not belong to the current user")

        succeeded, ref_id = self._verify_payment(request, txn)

        with db_transaction.atomic():
            # قفل ردیف تا دو درخواست هم‌زمان (یا رفرش صفحه) نتوانند دوبار پردازش کنند
            locked = Transaction.objects.select_for_update().select_related('order').get(pk=txn.pk)

            if locked.status != 'pending':
                # قبلاً پردازش شده؛ فقط نتیجه را دوباره نشان می‌دهیم، بدون هیچ عملیات جانبی.
                # این دقیقاً همان چیزی است که جلوی «ثبت سند دریافت وجه تکراری در هلو با هر
                # F5 روی صفحه‌ی بازگشت» را می‌گیرد.
                return render(request, 'payments/result.html', {
                    'transaction': locked, 'success': locked.status == 'success',
                })

            order = locked.order

            # مبلغ درگاه باید دقیقاً «باقی‌مانده پس از سهم کیف‌پول» باشد، نه کل سفارش (Wallet
            # Phase 4: در پرداخت ترکیبی سهم کیف‌پول از قبل کسر شده؛ درگاه فقط remaining را می‌بیند)
            expected_gateway_amount = order.total_price - locked.wallet_amount
            if succeeded and locked.amount != expected_gateway_amount:
                # مبلغ تراکنش با مبلغ سفارش نمی‌خواند؛ نباید بی‌سروصدا موفق ثبت شود
                logger.critical(
                    "ناهماهنگی مبلغ پرداخت: تراکنش %s مبلغ %s ولی سفارش #%s مبلغ %s (سهم کیف‌پول %s)",
                    locked.authority, locked.amount, order.id, order.total_price, locked.wallet_amount,
                )
                succeeded = False

            locked.status = 'success' if succeeded else 'failed'
            locked.ref_id = ref_id
            locked.save(update_fields=['status', 'ref_id', 'updated_at'])

            if not succeeded and locked.wallet_amount:
                # سهم کیف‌پول از قبل کسر شده بود (Mixed)؛ چون درگاه شکست خورد/کاربر منصرف شد،
                # باید بلافاصله با یک ردیف معکوس واقعی در لجر برگردد - نه صرفاً آزادسازی reserved.
                # این خط داخل همان گاردی است که بالاتر «status != 'pending'» را چک می‌کند، پس
                # به‌ازای هر Transaction فقط یک‌بار اجرا می‌شود - Idempotent در برابر Callback تکراری/Retry.
                checkout._reverse_wallet_leg_locked(
                    locked, reason=f'شکست/انصراف پرداخت درگاه سفارش #{order.id}',
                )

            if succeeded:
                # فقط بعد از commit موفق دیتابیس، وگرنه ممکن است پیامک «پرداخت شد» برای
                # تراکنشی برود که در نهایت rollback شده
                db_transaction.on_commit(
                    lambda: self._on_payment_succeeded(order, locked)
                )

        return render(request, 'payments/result.html', {'transaction': locked, 'success': succeeded})

    @staticmethod
    def _on_payment_succeeded(order, txn):
        """
        اعلام رویداد «پرداخت موفق شد».

        این اپ نمی‌داند در پی آن چه اتفاقی می‌افتد (ثبت سند دریافت وجه در حسابداری،
        پیامک به مشتری و مدیر، ...). send_robust یعنی خطای یک شنونده بقیه را متوقف نمی‌کند
        و مسیر پرداخت — که از نظر سایت همین الان قطعی ثبت شده — را هم نمی‌شکند.
        """
        results = payment_succeeded.send_robust(sender=Transaction, order=order, transaction=txn)
        for receiver, response in results:
            if isinstance(response, Exception):
                logger.exception(
                    "شنونده‌ی %s برای پرداخت موفق سفارش %s خطا داد: %s",
                    getattr(receiver, '__qualname__', receiver), order.id, response,
                    exc_info=response,
                )
