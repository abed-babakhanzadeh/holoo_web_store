from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic import TemplateView
from products.models import Product
from services.text import normalize_persian

SESSION_KEY = 'compare_ids'
MAX_COMPARE_ITEMS = 4


def _get_ids(request):
    return request.session.get(SESSION_KEY, [])


def _save_ids(request, ids):
    request.session[SESSION_KEY] = ids
    request.session.modified = True


class CompareToggleView(View):
    """ افزودن/حذف یک محصول از لیست مقایسه با یک کلیک (toggle)، بدون نیاز به لاگین """

    def post(self, request, product_id, *args, **kwargs):
        product = get_object_or_404(Product, id=product_id, is_active=True)
        ids = _get_ids(request)

        if product.id in ids:
            ids.remove(product.id)
            is_comparing = False
        else:
            if len(ids) >= MAX_COMPARE_ITEMS:
                ids.pop(0)
            ids.append(product.id)
            is_comparing = True

        _save_ids(request, ids)

        compact = request.POST.get('compact') == 'true'
        response = render(request, 'compare/partials/compare_button.html', {
            'product': product, 'is_comparing': is_comparing, 'compact': compact,
        })
        response['HX-Trigger'] = 'compareUpdated'
        return response


class CompareStatusView(View):
    """ برای هماهنگ نگه‌داشتن دکمه‌ی مقایسه با تغییراتی که از جای دیگر (مثلاً صفحه‌ی مقایسه) رخ می‌دهد """

    def get(self, request, product_id, *args, **kwargs):
        product = get_object_or_404(Product, id=product_id, is_active=True)
        compact = request.GET.get('compact') == 'true'
        return render(request, 'compare/partials/compare_button.html', {
            'product': product, 'is_comparing': product.id in _get_ids(request), 'compact': compact,
        })


class CompareRemoveView(View):
    """ حذف یک محصول از صفحه‌ی مقایسه """

    def post(self, request, product_id, *args, **kwargs):
        ids = _get_ids(request)
        if product_id in ids:
            ids.remove(product_id)
            _save_ids(request, ids)
        return redirect('compare:list')


class CompareClearView(View):
    """ خالی کردن کامل لیست مقایسه """

    def post(self, request, *args, **kwargs):
        _save_ids(request, [])
        return redirect('compare:list')


class CompareAddView(View):
    """ افزودن مستقیم یک محصول به لیست مقایسه از باکس جستجوی صفحه‌ی مقایسه (ریدایرکت به همان صفحه) """

    def post(self, request, product_id, *args, **kwargs):
        product = get_object_or_404(Product, id=product_id, is_active=True)
        ids = _get_ids(request)
        if product.id not in ids:
            if len(ids) >= MAX_COMPARE_ITEMS:
                ids.pop(0)
            ids.append(product.id)
            _save_ids(request, ids)
        return redirect('compare:list')


class CompareSearchView(TemplateView):
    """ جستجوی زنده‌ی داخل باکس «افزودن محصول» در صفحه‌ی مقایسه """
    template_name = 'compare/partials/search_results.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        query = self.request.GET.get('q', '').strip()
        products = []
        if query:
            products = Product.objects.filter(
                is_active=True, name_normalized__icontains=normalize_persian(query)
            ).select_related('category')[:8]
        context['query'] = query
        context['products'] = products
        context['compare_ids'] = _get_ids(self.request)
        context['max_items'] = MAX_COMPARE_ITEMS
        return context


class CompareBadgeView(TemplateView):
    """ بج شمارنده‌ی مقایسه در هدر (مثل بج سبد خرید) """
    template_name = 'compare/partials/compare_badge.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['compare_count'] = len(_get_ids(self.request))
        return context


class CompareListView(TemplateView):
    """ صفحه‌ی مقایسه: کارت‌های محصولات انتخاب‌شده + جدول تطبیق مشخصات فنی """
    template_name = 'compare/compare.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        ids = _get_ids(self.request)
        products = list(
            Product.objects.filter(id__in=ids, is_active=True)
            .select_related('category', 'brand')
            .prefetch_related('features__feature')
        )
        # حفظ ترتیبی که کاربر محصولات را اضافه کرده
        products.sort(key=lambda p: ids.index(p.id))

        # اجتماع تمام نام‌های ویژگی موجود در محصولات انتخاب‌شده، برای ساخت ردیف‌های جدول
        feature_names = []
        seen = set()
        product_feature_maps = []
        for product in products:
            feature_map = {pfv.feature.name: pfv.value for pfv in product.features.all()}
            product_feature_maps.append(feature_map)
            for name in feature_map:
                if name not in seen:
                    seen.add(name)
                    feature_names.append(name)

        feature_rows = [
            {
                'name': name,
                'values': [feature_map.get(name, '—') for feature_map in product_feature_maps],
            }
            for name in feature_names
        ]

        context['products'] = products
        context['feature_rows'] = feature_rows
        context['max_items'] = MAX_COMPARE_ITEMS
        context['can_add_more'] = len(products) < MAX_COMPARE_ITEMS
        return context
