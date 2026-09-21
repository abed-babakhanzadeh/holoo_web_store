"""
صفحات پنل کاربری برای کدهای تخفیف: «کدهای تخفیف من» و «دریافت کد تخفیف جدید».

همه‌ی صفحه‌ها فقط با request.user کار می‌کنند و هیچ شناسه‌ی کاربر/کدی از ورودی خوانده نمی‌شود، مگر شناسه‌ی کوپن در آدرسِ
دریافت که سرور آن را با شروط قابل‌دریافت‌بودن و واجد‌شرایط‌بودن می‌سنجد (نگاه کنید wallet.claim).
"""

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views import View
from django.views.generic import TemplateView

from . import wallet


class MyCouponsView(LoginRequiredMixin, TemplateView):
    template_name = 'promotions/panel/my_codes.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        tabs = wallet.my_codes(self.request.user)
        context.update({
            'tabs': tabs,
            'active_nav': 'discounts',
            'first_tab': self.request.GET.get('tab') if self.request.GET.get('tab') in ('active', 'used', 'expired') else 'active',
        })
        return context


class GetCouponView(LoginRequiredMixin, TemplateView):
    template_name = 'promotions/panel/get_code.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({'cards': wallet.claimable_coupons(self.request.user), 'active_nav': 'discounts'})
        return context


class ClaimCouponView(LoginRequiredMixin, View):
    """
    دریافت یک کد (فقط POST). با HTMX: پنجره‌ی نتیجه + بروزرسانی همان کارت؛ بدون JS: پیام و هدایت.
    هر حالتِ غیرقابل‌دریافت پیام یکسان دارد؛ کدِ اختصاصیِ دیگران با حدس شناسه قابل‌کشف نیست.
    """

    def post(self, request, pk):
        result = wallet.claim(request.user, pk)
        if request.headers.get('HX-Request'):
            return render(request, 'promotions/panel/partials/claim_result.html', {'result': result, 'card_pk': pk})
        if result.ok:
            messages.success(request, result.message)
            return redirect(reverse('promotions:my_codes'))
        messages.error(request, result.message)
        return redirect(reverse('promotions:get_code'))
