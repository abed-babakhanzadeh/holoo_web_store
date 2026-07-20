from datetime import timedelta
from urllib.parse import urlencode

from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from products.models import Product
from .models import Review, ReviewImage, ReviewPoint, _is_verified_purchase, _get_purchased_color

MAX_REVIEW_IMAGES = 3
MIN_BODY_LENGTH = 20

REVIEW_SORT_OPTIONS = {
    'newest': ('-created_at',),
    'oldest': ('created_at',),
    'rating_high': ('-rating', '-created_at'),
    'rating_low': ('rating', '-created_at'),
}


def _save_points(review, pros, cons):
    review.points.all().delete()
    points = [ReviewPoint(review=review, kind='pro', text=t.strip()) for t in pros if t.strip()]
    points += [ReviewPoint(review=review, kind='con', text=t.strip()) for t in cons if t.strip()]
    if points:
        ReviewPoint.objects.bulk_create(points)


def _product_redirect(product, error=None, submitted=False):
    url = reverse('products:product_detail', args=[product.slug])
    params = {}
    if error:
        params['review_error'] = error
    if submitted:
        params['review'] = 'submitted'
    if params:
        url = f"{url}?{urlencode(params)}#Comments"
    else:
        url = f"{url}#Comments"
    return redirect(url)


class ReviewCreateView(LoginRequiredMixin, View):
    """ ثبت نظر اصلی (امتیاز + متن + نقاط قوت/ضعف + تصاویر) روی یک محصول """

    def post(self, request, product_slug, *args, **kwargs):
        product = get_object_or_404(Product, slug=product_slug, is_active=True)

        existing = Review.objects.filter(product=product, user=request.user, parent__isnull=True).first()
        if existing:
            return redirect('reviews:edit', review_id=existing.id)

        rating = request.POST.get('rating')
        body = request.POST.get('body', '').strip()
        title = request.POST.get('title', '').strip()

        if rating not in ('1', '2', '3', '4', '5'):
            return _product_redirect(product, error='rating_required')
        if len(body) < MIN_BODY_LENGTH:
            return _product_redirect(product, error='body_too_short')

        review = Review.objects.create(
            product=product, user=request.user, rating=int(rating), title=title, body=body,
            status='pending', is_verified_purchase=_is_verified_purchase(request.user, product),
            color=_get_purchased_color(request.user, product),
        )
        _save_points(review, request.POST.getlist('pros'), request.POST.getlist('cons'))

        for f in request.FILES.getlist('images')[:MAX_REVIEW_IMAGES]:
            ReviewImage.objects.create(review=review, image=f)

        return _product_redirect(product, submitted=True)


class ReviewReplyView(LoginRequiredMixin, View):
    """ پاسخ کاربر به یک نظر یا به یک پاسخ دیگر (بحث تو در تو)؛ مثل نظر اصلی در صف تایید قرار می‌گیرد """

    def post(self, request, review_id, *args, **kwargs):
        parent = get_object_or_404(Review, id=review_id, status='published')
        body = request.POST.get('body', '').strip()
        if not body:
            return render(request, 'reviews/partials/review_node.html', {'review': parent, 'show_status': True}, status=400)

        reply = Review.objects.create(
            product=parent.product, user=request.user, parent=parent, body=body, status='pending',
        )
        return render(request, 'reviews/partials/review_node.html', {'review': reply, 'show_status': True})


class ReviewEditView(LoginRequiredMixin, View):
    """ ویرایش نظر اصلی خود کاربر (فقط تا وقتی تایید نهایی نشده)؛ ویرایش دوباره آن را در صف تایید می‌گذارد """
    template_name = 'reviews/edit_review.html'

    def get(self, request, review_id, *args, **kwargs):
        review = get_object_or_404(Review, id=review_id, user=request.user, parent__isnull=True)
        if not review.can_edit:
            return redirect('reviews:my_reviews')
        return render(request, self.template_name, {
            'review': review,
            'pros': review.points.filter(kind='pro'),
            'cons': review.points.filter(kind='con'),
        })

    def post(self, request, review_id, *args, **kwargs):
        review = get_object_or_404(Review, id=review_id, user=request.user, parent__isnull=True)
        if not review.can_edit:
            return redirect('reviews:my_reviews')

        rating = request.POST.get('rating')
        body = request.POST.get('body', '').strip()
        title = request.POST.get('title', '').strip()

        if rating not in ('1', '2', '3', '4', '5') or len(body) < MIN_BODY_LENGTH:
            return render(request, self.template_name, {
                'review': review,
                'pros': review.points.filter(kind='pro'),
                'cons': review.points.filter(kind='con'),
                'error': 'rating_required' if rating not in ('1', '2', '3', '4', '5') else 'body_too_short',
            })

        review.rating = int(rating)
        review.title = title
        review.body = body
        review.status = 'pending'
        review.rejection_reason = ''
        review.save()

        _save_points(review, request.POST.getlist('pros'), request.POST.getlist('cons'))

        remove_ids = set(request.POST.getlist('remove_image'))
        if remove_ids:
            review.images.filter(id__in=remove_ids).delete()

        remaining_slots = MAX_REVIEW_IMAGES - review.images.count()
        for f in request.FILES.getlist('images')[:max(remaining_slots, 0)]:
            ReviewImage.objects.create(review=review, image=f)

        return redirect('reviews:my_reviews')


def _delete_review_tree(review):
    """
    حذف بازگشتی از پایین به بالا (پاسخ‌ها قبل از خودِ نظر).
    چون parent یک FK خودارجاع است، اگر بخواهیم با یک review.delete() روی ریشه
    کل زیردرخت را یک‌جا کسکید کنیم، کالکتور جنگو برای حل ترتیب حذف در بیش از یک
    سطح از خودارجاعی، ابتدا ستون parent را روی برخی ردیف‌ها موقتاً NULL می‌کند که
    با UniqueConstraint شرطی‌مان (یک نظر اصلی به‌ازای هر کاربر/محصول) تداخل ایجاد
    می‌کند. حذف برگ‌به‌ریشه این مشکل را کامل دور می‌زند.
    """
    for child in list(review.replies.all()):
        _delete_review_tree(child)
    review.delete()


class ReviewDeleteView(LoginRequiredMixin, View):
    """ حذف نظر/پاسخ خودِ کاربر (حذف نظر اصلی، پاسخ‌های زیرش را هم حذف می‌کند) """

    def post(self, request, review_id, *args, **kwargs):
        review = get_object_or_404(Review, id=review_id, user=request.user)
        redirect_to = request.META.get('HTTP_REFERER') or reverse('reviews:my_reviews')
        _delete_review_tree(review)
        return redirect(redirect_to)


class MyReviewListView(LoginRequiredMixin, TemplateView):
    """ لیست نظرات ثبت‌شده‌ی خودِ کاربر در پنل کاربری، با فیلتر وضعیت/امتیاز/بازه‌ی زمانی """
    template_name = 'reviews/my_reviews.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        reviews = Review.objects.filter(user=self.request.user, parent__isnull=True).select_related('product') \
            .prefetch_related('points', 'images', 'replies__user')

        status = self.request.GET.get('status', '')
        rating = self.request.GET.get('rating', '')
        date_range = self.request.GET.get('date_range', '')

        if status:
            reviews = reviews.filter(status=status)
        if rating:
            reviews = reviews.filter(rating=rating)
        if date_range:
            days_map = {'today': 1, 'week': 7, 'month': 30, 'year': 365}
            days = days_map.get(date_range)
            if days:
                reviews = reviews.filter(created_at__gte=timezone.now() - timedelta(days=days))

        sort = self.request.GET.get('sort', 'newest')
        reviews = reviews.order_by(*REVIEW_SORT_OPTIONS.get(sort, REVIEW_SORT_OPTIONS['newest']))

        context['active_nav'] = 'reviews'
        context['reviews'] = reviews
        context['selected_status'] = status
        context['selected_rating'] = rating
        context['selected_date_range'] = date_range
        context['reviews_sort'] = sort
        context['status_choices'] = Review.STATUS_CHOICES
        return context
