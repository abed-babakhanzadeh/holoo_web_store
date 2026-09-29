"""
سطح وب باشگاه مشتریان (Loyalty Phase 4D-2) - فقط Expose کردن سرویس‌های تثبیت‌شده‌ی فازهای
۴A/۴B (loyalty/redemption.py::redeem_points_to_wallet، loyalty/reward_redemption.py::
redeem_points_for_reward). این فایل هیچ منطق مالی/کسب‌وکاری‌ای (کسر امتیاز، بررسی ظرفیت،
اعتبارسنجی سقف) ندارد - فقط ورودی وب را جمع می‌کند، سرویس را صدا می‌زند، و استثنا را به پیام
کاربرپسند نگاشت می‌کند.

الگوی هر دو ویوی بازخرید (PRG + پشتیبانی اختیاری HTMX + پیام‌های Django) عیناً از
promotions/views.py::ClaimCouponView و wallet/views.py گرفته شده - هیچ سیستم طراحی/الگوی تازه‌ای
ساخته نشده. ایدمپوتنسی سطح وب عیناً الگوی loyalty/admin.py::ManualAdjustmentForm است: یک توکن
یک‌بارمصرف که فقط در GET (رندر اول) تولید و در یک HiddenInput رفت‌وبرگشت می‌کند.

امنیت مالکیت: در همه‌جا فقط request.user به سرویس پاس داده می‌شود؛ هیچ شناسه‌ی کاربر از
URL/POST خوانده نمی‌شود (دقیقاً هم‌قرارداد promotions/views.py).
"""

import logging
import uuid

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic import TemplateView

from products.models import SiteSettings

from .exceptions import (
    IdempotencyKeyConflictError, InsufficientPointsError, RewardAlreadyRedeemedError,
    RewardInactiveError, RewardOutOfStockError,
)
from .forms import RedeemPointsForm
from .models import LoyaltyAccount, LoyaltyReward, LoyaltyTier, LoyaltyTierHistory, LoyaltyTransaction
from .redemption import RedemptionValidationError, redeem_points_to_wallet
from .reward_redemption import redeem_points_for_reward
from .services import get_dynamic_tier_for_user

logger = logging.getLogger(__name__)

RESULT_PARTIAL = 'loyalty/partials/redeem_result.html'
SEEN_TIER_UPGRADE_SESSION_KEY = 'seen_tier_upgrade_id'


def _next_tier(current_tier):
    """ اولین سطح فعال بعد از سطح فعلی (بر مبنای rank)؛ اگر کاربر بالاترین سطح را دارد یا هیچ سطحی تعریف نشده، None. """
    if current_tier is None:
        return LoyaltyTier.objects.filter(is_active=True).order_by('rank').first()
    return LoyaltyTier.objects.filter(is_active=True, rank__gt=current_tier.rank).order_by('rank').first()


def _tier_progress_percent(current_tier, next_tier, lifetime_earned):
    """ درصد پیشرفت تا سطح بعدی - صرفاً محاسبه‌ی نمایشی روی داده‌ی از قبل خوانده‌شده، هم‌الگوی
    accounts.models.CustomUser.get_loyalty_progress_percent برای سیستم سنتی. """
    if next_tier is None:
        return 100
    current_threshold = current_tier.threshold if current_tier else 0
    span = next_tier.threshold - current_threshold
    if span <= 0:
        return 100
    progress = (lifetime_earned - current_threshold) / span * 100
    return max(0, min(100, round(progress)))


class LoyaltyDashboardView(LoginRequiredMixin, TemplateView):
    """ پیشخوان باشگاه: موجودی/کل کسب‌شده/کل مصرف‌شده، سطح داینامیک + پیشرفت، تاریخچه‌ی صفحه‌بندی‌شده. """
    template_name = 'loyalty/dashboard.html'
    PAGE_SIZE = 10

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        club_activated = bool(SiteSettings.cached().loyalty_activated_at)

        account = LoyaltyAccount.objects.filter(user=user).first()
        current_balance = account.current_balance if account else 0
        lifetime_earned = account.lifetime_earned if account else 0
        lifetime_redeemed = account.lifetime_redeemed if account else 0

        current_tier = get_dynamic_tier_for_user(user)
        next_tier = _next_tier(current_tier)
        points_to_next_tier = max(0, next_tier.threshold - lifetime_earned) if next_tier else 0

        transactions = LoyaltyTransaction.objects.filter(account__user=user)
        page_obj = Paginator(transactions, self.PAGE_SIZE).get_page(self.request.GET.get('page'))

        # Loyalty Phase 6B - تبریک ارتقای رتبه: بدون هیچ فیلد/مایگریشن تازه، فقط با سشن. اگر
        # آخرین ردیف LoyaltyTierHistory این کاربر با آخرین pk دیده‌شده در همین سشن فرق داشت،
        # یک‌بار مودال نشان داده می‌شود و سشن به‌روزرسانی می‌گردد تا در بازدیدهای بعدی تکرار نشود.
        latest_history = LoyaltyTierHistory.objects.filter(account__user=user).order_by('-created_at').first()
        newly_upgraded_tier = None
        if latest_history and self.request.session.get(SEEN_TIER_UPGRADE_SESSION_KEY) != latest_history.pk:
            newly_upgraded_tier = latest_history.new_tier
            self.request.session[SEEN_TIER_UPGRADE_SESSION_KEY] = latest_history.pk

        context.update({
            'active_nav': 'loyalty',
            'club_activated': club_activated,
            'current_balance': current_balance,
            'lifetime_earned': lifetime_earned,
            'lifetime_redeemed': lifetime_redeemed,
            'current_tier': current_tier,
            'next_tier': next_tier,
            'points_to_next_tier': points_to_next_tier,
            'newly_upgraded_tier': newly_upgraded_tier,
            'progress_percent': _tier_progress_percent(current_tier, next_tier, lifetime_earned),
            'page_obj': page_obj,
            'transactions': page_obj.object_list,
        })
        return context


class RewardCatalogView(LoginRequiredMixin, TemplateView):
    """ کاتالوگ پاداش‌های فعال؛ هر کارت یک فرم POST مستقل با توکن ایدمپوتنسی خودش دارد (هم‌الگوی
    promotions/views.py::GetCouponView + templates/promotions/panel/get_code.html). """
    template_name = 'loyalty/rewards.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        rewards = list(LoyaltyReward.objects.filter(is_active=True).select_related('coupon').order_by('display_order', 'id'))
        for reward in rewards:
            reward.idempotency_token = uuid.uuid4().hex   # فقط در همین رندر GET تولید می‌شود

        account = LoyaltyAccount.objects.filter(user=self.request.user).first()
        context.update({
            'active_nav': 'loyalty',
            'club_activated': bool(SiteSettings.cached().loyalty_activated_at),
            'rewards': rewards,
            'current_balance': account.current_balance if account else 0,
        })
        return context


class RedeemToWalletView(LoginRequiredMixin, TemplateView):
    """ فرم تبدیل امتیاز به کیف‌پول (loyalty/redemption.py::redeem_points_to_wallet). """
    template_name = 'loyalty/redeem_wallet.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        settings_obj = SiteSettings.cached()
        account = LoyaltyAccount.objects.filter(user=self.request.user).first()
        context.update({
            'active_nav': 'loyalty',
            'club_activated': bool(settings_obj.loyalty_activated_at),
            'settings': settings_obj,
            'form': RedeemPointsForm(),
            'current_balance': account.current_balance if account else 0,
        })
        return context

    def post(self, request, *args, **kwargs):
        settings_obj = SiteSettings.cached()
        if not settings_obj.loyalty_activated_at:
            return self._respond_error(request, 'باشگاه مشتریان هنوز فعال نشده است.')

        form = RedeemPointsForm(request.POST)
        if not form.is_valid():
            return self._respond_error(request, 'تعداد امتیاز واردشده نامعتبر است.')

        points = form.cleaned_data['points']
        token = form.cleaned_data['idempotency_token']
        key = f'web-redeem-wallet-{request.user.id}-{token}'

        try:
            _, wallet_txn = redeem_points_to_wallet(request.user, points, idempotency_key=key)
        except InsufficientPointsError:
            return self._respond_error(request, 'موجودی امتیاز شما برای این عملیات کافی نیست.')
        except RedemptionValidationError as exc:
            return self._respond_error(request, str(exc))
        except IdempotencyKeyConflictError:
            logger.warning('تعارض کلید ایدمپوتنسی در تبدیل امتیاز به کیف‌پول - user_id=%s', request.user.id)
            return self._respond_error(request, 'درخواست نامعتبر است یا قبلاً ارسال شده است. لطفاً صفحه را رفرش کنید.')

        message = f'{points} امتیاز با موفقیت به {wallet_txn.amount:.0f} تومان کیف‌پول تبدیل شد.'
        return self._respond_success(request, message)

    def _respond_success(self, request, message):
        messages.success(request, message)
        if request.headers.get('HX-Request'):
            return render(request, RESULT_PARTIAL, {'ok': True, 'message': message})
        return redirect('loyalty:redeem_wallet')

    def _respond_error(self, request, message):
        messages.error(request, message)
        if request.headers.get('HX-Request'):
            return render(request, RESULT_PARTIAL, {'ok': False, 'message': message})
        return redirect('loyalty:redeem_wallet')


class RedeemRewardView(LoginRequiredMixin, View):
    """ بازخرید یک پاداش کاتالوگ (loyalty/reward_redemption.py::redeem_points_for_reward) - اکیداً
    POST-only (کلاس فقط post را تعریف می‌کند؛ GET/دیگر متدها خودکار ۴۰۵ می‌گیرند). """

    def post(self, request, pk, *args, **kwargs):
        reward = get_object_or_404(LoyaltyReward, pk=pk, is_active=True)

        if not SiteSettings.cached().loyalty_activated_at:
            return self._respond_error(request, reward, 'باشگاه مشتریان هنوز فعال نشده است.')

        token = (request.POST.get('idempotency_token') or '').strip()
        if not token:
            return self._respond_error(request, reward, 'درخواست نامعتبر است. لطفاً صفحه را رفرش کنید.')
        key = f'web-redeem-reward-{request.user.id}-{reward.pk}-{token}'

        try:
            _, user_coupon = redeem_points_for_reward(request.user, reward, idempotency_key=key)
        except InsufficientPointsError:
            return self._respond_error(request, reward, 'موجودی امتیاز شما برای دریافت این پاداش کافی نیست.')
        except RewardInactiveError as exc:
            return self._respond_error(request, reward, str(exc))
        except RewardOutOfStockError:
            return self._respond_error(request, reward, 'ظرفیت این پاداش تکمیل شده است.')
        except RewardAlreadyRedeemedError:
            return self._respond_error(request, reward, 'شما قبلاً این پاداش را دریافت کرده‌اید.')
        except IdempotencyKeyConflictError:
            logger.warning(
                'تعارض کلید ایدمپوتنسی در بازخرید پاداش - user_id=%s reward_id=%s', request.user.id, reward.pk,
            )
            return self._respond_error(request, reward, 'درخواست نامعتبر است یا قبلاً ارسال شده است. لطفاً صفحه را رفرش کنید.')

        message = f'پاداش «{reward.title}» با موفقیت دریافت شد؛ کد آن در «کدهای تخفیف من» موجود است.'
        return self._respond_success(request, reward, message, user_coupon.coupon.code)

    def _respond_success(self, request, reward, message, code=''):
        messages.success(request, message)
        if request.headers.get('HX-Request'):
            return render(request, RESULT_PARTIAL, {'ok': True, 'message': message, 'reward': reward, 'code': code})
        return redirect('loyalty:rewards')

    def _respond_error(self, request, reward, message):
        messages.error(request, message)
        if request.headers.get('HX-Request'):
            return render(request, RESULT_PARTIAL, {'ok': False, 'message': message, 'reward': reward})
        return redirect('loyalty:rewards')
