from django.http import HttpResponse
from django.views import View
from django.views.generic import TemplateView
from django.contrib.auth.mixins import LoginRequiredMixin
from .models import RecentlyViewed


class RecentlyViewedListView(LoginRequiredMixin, TemplateView):
    """ صفحه‌ی بازدیدهای اخیر کاربر در پنل کاربری """
    template_name = 'recently_viewed/list.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['active_nav'] = 'recently_viewed'
        context['items'] = RecentlyViewed.objects.filter(user=self.request.user).select_related('product')
        return context


class RemoveRecentlyViewedView(LoginRequiredMixin, View):
    """ حذف یک محصول از تاریخچه‌ی بازدید """

    def post(self, request, product_id, *args, **kwargs):
        RecentlyViewed.objects.filter(user=request.user, product_id=product_id).delete()
        return HttpResponse('')


class ClearRecentlyViewedView(LoginRequiredMixin, View):
    """ پاک کردن کامل تاریخچه‌ی بازدید کاربر """

    def post(self, request, *args, **kwargs):
        RecentlyViewed.objects.filter(user=request.user).delete()
        response = HttpResponse('')
        response['HX-Refresh'] = 'true'
        return response
