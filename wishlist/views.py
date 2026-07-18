from django.shortcuts import render, get_object_or_404
from django.http import HttpResponse
from django.views import View
from django.views.generic import TemplateView
from django.contrib.auth.mixins import LoginRequiredMixin
from products.models import Product
from .models import FavoriteProduct


class ToggleFavoriteView(LoginRequiredMixin, View):
    """ افزودن/حذف یک محصول از علاقه‌مندی‌ها با یک کلیک (toggle) """

    def post(self, request, product_id, *args, **kwargs):
        product = get_object_or_404(Product, id=product_id, is_active=True)
        favorite = FavoriteProduct.objects.filter(user=request.user, product=product).first()
        if favorite:
            favorite.delete()
            is_favorited = False
        else:
            FavoriteProduct.objects.create(user=request.user, product=product)
            is_favorited = True

        compact = request.POST.get('compact') == 'true'
        response = render(request, 'wishlist/partials/favorite_button.html', {
            'product': product, 'is_favorited': is_favorited, 'compact': compact,
        })
        response['HX-Trigger'] = 'favoritesUpdated'
        return response


class FavoriteStatusView(LoginRequiredMixin, View):
    """ برای هماهنگ نگه‌داشتن دکمه‌ی علاقه‌مندی محصول با تغییراتی که از جای دیگر (مثلاً صفحه‌ی لیست علاقه‌مندی‌ها) رخ می‌دهد """

    def get(self, request, product_id, *args, **kwargs):
        product = get_object_or_404(Product, id=product_id, is_active=True)
        is_favorited = FavoriteProduct.objects.filter(user=request.user, product=product).exists()
        compact = request.GET.get('compact') == 'true'
        return render(request, 'wishlist/partials/favorite_button.html', {
            'product': product, 'is_favorited': is_favorited, 'compact': compact,
        })


class RemoveFromWishlistView(LoginRequiredMixin, View):
    """ حذف مستقیم یک محصول از صفحه‌ی لیست علاقه‌مندی‌ها (بدون بازگرداندن دکمه‌ی toggle) """

    def post(self, request, product_id, *args, **kwargs):
        FavoriteProduct.objects.filter(user=request.user, product_id=product_id).delete()
        return HttpResponse('')


class WishlistListView(LoginRequiredMixin, TemplateView):
    """ صفحه‌ی لیست علاقه‌مندی‌های کاربر در پنل کاربری """
    template_name = 'wishlist/list.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['active_nav'] = 'wishlist'
        context['favorites'] = FavoriteProduct.objects.filter(user=self.request.user).select_related('product')
        return context
