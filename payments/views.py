import uuid
import random
from django.shortcuts import render, redirect, get_object_or_404
from django.views import View
from django.views.generic import TemplateView
from django.contrib.auth.mixins import LoginRequiredMixin
from django.conf import settings
from django.utils import timezone

from orders.models import Order
from .models import Transaction
from services.sms import send_sms
from holoo.tasks import confirm_payment_in_holoo

class PaymentStartView(LoginRequiredMixin, View):
    """ شروع فرآیند پرداخت و انتقال به درگاه (Mock) """

    def get(self, request, order_id, *args, **kwargs):
        # فقط سفارشات معتبر که پرداخت نشده‌اند (یا پرداخت قبلی‌شان ناموفق بوده)
        order = get_object_or_404(Order, id=order_id, user=request.user)
        if not order.can_pay:
            return redirect('orders:order_history')

        # تولید یک اتوریتی شبیه‌سازی شده (در دنیای واقعی این را از API بانک می‌گیریم)
        authority = f"A00000000000000000000000000{random.randint(10000, 99999)}"
        
        # ثبت تراکنش در دیتابیس
        Transaction.objects.create(
            user=request.user,
            order=order,
            amount=order.total_price,
            authority=authority
        )

        # انتقال به صفحه درگاه شبیه‌سازی شده
        return redirect('payments:mock_gateway', authority=authority)


class MockGatewayView(TemplateView):
    """ صفحه شبیه‌ساز درگاه پرداخت (مثل صفحه زرین‌پال) """
    template_name = 'payments/mock_gateway.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        authority = self.kwargs['authority']
        context['transaction'] = get_object_or_404(Transaction, authority=authority, status='pending')
        return context


class PaymentCallbackView(View):
    """ بازگشت از درگاه پرداخت و انجام عملیات نهایی (پیامک + هلو) """
    
    def get(self, request, *args, **kwargs):
        authority = request.GET.get('Authority')
        status = request.GET.get('Status') # می‌تواند OK یا NOK باشد
        
        transaction = get_object_or_404(Transaction, authority=authority)
        order = transaction.order

        if status == 'OK':
            # ۱. آپدیت تراکنش
            transaction.status = 'success'
            transaction.ref_id = str(random.randint(10000000, 99999999)) # شماره پیگیری فرضی بانک
            transaction.save()

            # ۲. ارسال پیامک به مشتری
            customer_msg = f"مشتری گرامی {order.user.first_name}، پرداخت مبلغ {order.total_price:,.0f} تومان با موفقیت انجام شد. کد پیگیری: {transaction.ref_id}"
            send_sms(order.user.phone_number, customer_msg)

            # ۳. ارسال پیامک به مدیر
            admin_msg = f"تراکنش جدید! سفارش #{order.id} به مبلغ {order.total_price:,.0f} تومان توسط {order.user.phone_number} با موفقیت پرداخت شد."
            send_sms(settings.ADMIN_PHONE_NUMBER, admin_msg)

            # ۴. فاکتور از قبل (مستقل از نتیجه پرداخت) در هلو ثبت شده؛ حالا باید سند دریافت وجه ثبت شود.
            # این کار در پس‌زمینه (سلری) انجام می‌شود و وضعیت سفارش فقط پس از پاسخ هلو تغییر می‌کند.
            confirm_payment_in_holoo.delay(order.id)

            return render(request, 'payments/result.html', {'transaction': transaction, 'success': True})
            
        else:
            # پرداخت ناموفق
            transaction.status = 'failed'
            transaction.save()
            return render(request, 'payments/result.html', {'transaction': transaction, 'success': False})