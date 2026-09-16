import logging
import secrets
from django.db import transaction as db_transaction
from django.http import Http404
from django.shortcuts import render, redirect, get_object_or_404
from django.views import View
from django.contrib.auth.mixins import LoginRequiredMixin

from orders.models import Order
from .models import Transaction
from .signals import payment_succeeded

logger = logging.getLogger(__name__)


class PaymentStartView(LoginRequiredMixin, View):
    """ شروع فرآیند پرداخت و انتقال به درگاه (Mock) """

    def get(self, request, order_id, *args, **kwargs):
        # فقط سفارشات معتبر که پرداخت نشده‌اند (یا پرداخت قبلی‌شان ناموفق بوده)
        order = get_object_or_404(Order, id=order_id, user=request.user)
        if not order.can_pay:
            return redirect('orders:order_history')

        # تراکنشِ در انتظارِ قبلی (مثلاً کاربر وسط راه برگشته و دوباره «پرداخت» زده) دوباره
        # استفاده می‌شود؛ وگرنه با هر کلیک یک ردیف pending بی‌استفاده در دیتابیس تلنبار می‌شد
        pending = Transaction.objects.filter(order=order, user=request.user, status='pending').first()
        if pending:
            return redirect('payments:mock_gateway', authority=pending.authority)

        # تولید یک اتوریتی شبیه‌سازی شده (در دنیای واقعی این را از API بانک می‌گیریم).
        # secrets و نه random: این رشته نقش کلید دسترسی به صفحه‌ی بازگشت از درگاه را دارد،
        # پس باید غیرقابل‌حدس باشد — random در پایتون قابل پیش‌بینی است.
        authority = f"A{secrets.token_hex(16).upper()}"

        Transaction.objects.create(
            user=request.user,
            order=order,
            amount=order.total_price,
            authority=authority,
        )

        return redirect('payments:mock_gateway', authority=authority)


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

            if succeeded and locked.amount != order.total_price:
                # مبلغ تراکنش با مبلغ سفارش نمی‌خواند؛ نباید بی‌سروصدا موفق ثبت شود
                logger.critical(
                    "ناهماهنگی مبلغ پرداخت: تراکنش %s مبلغ %s ولی سفارش #%s مبلغ %s",
                    locked.authority, locked.amount, order.id, order.total_price,
                )
                succeeded = False

            locked.status = 'success' if succeeded else 'failed'
            locked.ref_id = ref_id
            locked.save(update_fields=['status', 'ref_id', 'updated_at'])

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
