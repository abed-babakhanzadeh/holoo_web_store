from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.db.models import Count, F, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import DetailView

from .models import BlogCategory, Post, PostComment, PostCommentLike

POSTS_PER_PAGE = 9
MIN_COMMENT_LENGTH = 2


def _sidebar_context():
    """ زمینه‌ی مشترک سایدبار (دسته‌ها با شمارش واقعی + پرطرفدارترین‌ها)، هم برای لیست هم تک‌مقاله """
    blog_categories = BlogCategory.objects.filter(is_active=True).annotate(
        post_count=Count('posts', filter=Q(posts__status='published', posts__published_at__lte=timezone.now()))
    ).order_by('name')
    popular_posts = Post.visible.order_by('-views_count')[:3]
    return {
        'blog_categories': blog_categories,
        'popular_posts': popular_posts,
    }


class PostListView(View):
    """ صفحه لیست/آرشیو مقالات وبلاگ با فیلتر دسته/برچسب/جستجو و صفحه‌بندی htmx """

    def get(self, request, *args, **kwargs):
        posts = Post.visible.select_related('category', 'author').prefetch_related('tags').order_by('-published_at', 'id')

        category_slug = request.GET.get('category')
        if category_slug:
            posts = posts.filter(category__slug=category_slug)

        tag_slug = request.GET.get('tag')
        if tag_slug:
            posts = posts.filter(tags__slug=tag_slug)

        search_query = request.GET.get('q', '').strip()
        if search_query:
            posts = posts.filter(Q(title__icontains=search_query) | Q(excerpt__icontains=search_query))

        paginator = Paginator(posts.distinct(), POSTS_PER_PAGE)
        page_number = request.GET.get('page', 1)
        page_obj = paginator.get_page(page_number)
        elided_page_range = list(page_obj.paginator.get_elided_page_range(page_obj.number, on_each_side=1, on_ends=1))

        querydict = request.GET.copy()
        querydict.pop('page', None)
        base_qs = querydict.urlencode()

        context = {
            'posts': page_obj,
            'page_obj': page_obj,
            'elided_page_range': elided_page_range,
            'base_qs': base_qs,
            'current_category': category_slug,
            'current_tag': tag_slug,
            'search_query': search_query,
        }
        context.update(_sidebar_context())

        if request.headers.get('HX-Request'):
            return render(request, 'blog/partials/post_grid.html', context)
        return render(request, 'blog/list.html', context)


class PostDetailView(DetailView):
    """ صفحه تک مقاله وبلاگ: بدنه، نویسنده، تگ‌ها، مقالات مرتبط و نظرات """
    model = Post
    template_name = 'blog/detail.html'
    context_object_name = 'post'

    def get_queryset(self):
        return Post.visible.select_related('category', 'author').prefetch_related('tags')

    def get(self, request, *args, **kwargs):
        response = super().get(request, *args, **kwargs)
        Post.objects.filter(pk=self.object.pk).update(views_count=F('views_count') + 1)
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        context['related_posts'] = Post.visible.filter(
            category_id=self.object.category_id
        ).exclude(id=self.object.id).select_related('category')[:2] if self.object.category_id else \
            Post.visible.exclude(id=self.object.id).select_related('category')[:2]

        comments = self.object.comments.filter(parent__isnull=True)
        if self.request.user.is_authenticated:
            comments = comments.filter(Q(status='published') | Q(user=self.request.user))
        else:
            comments = comments.filter(status='published')
        context['comments'] = comments.select_related('user').prefetch_related(
            'replies__user', 'replies__likes', 'likes'
        ).order_by('-created_at')

        context['comment_count'] = PostComment.objects.filter(post=self.object, status='published').count()
        context['can_comment'] = self.request.user.is_authenticated
        context['comment_error'] = self.request.GET.get('comment_error') == '1'
        context['comment_submitted'] = self.request.GET.get('comment') == 'submitted'

        if self.request.user.is_authenticated:
            context['liked_comment_ids'] = set(
                PostCommentLike.objects.filter(user=self.request.user, comment__post=self.object).values_list('comment_id', flat=True)
            )
        else:
            context['liked_comment_ids'] = set()

        context.update(_sidebar_context())
        return context


class PostCommentCreateView(LoginRequiredMixin, View):
    """ ثبت نظر اصلی (فقط متن) روی یک مقاله؛ مثل نظر محصول در صف تایید قرار می‌گیرد """

    def post(self, request, post_slug, *args, **kwargs):
        post = get_object_or_404(Post.visible, slug=post_slug)
        body = request.POST.get('body', '').strip()

        url = reverse('blog:detail', args=[post.slug])
        if len(body) < MIN_COMMENT_LENGTH:
            return redirect(f"{url}?comment_error=1#comments")

        PostComment.objects.create(post=post, user=request.user, body=body, status='pending')
        return redirect(f"{url}?comment=submitted#comments")


class PostCommentReplyView(LoginRequiredMixin, View):
    """ پاسخ کاربر به یک نظر (بحث تو در تو)؛ مثل نظر اصلی در صف تایید قرار می‌گیرد """

    def post(self, request, comment_id, *args, **kwargs):
        parent = get_object_or_404(PostComment, id=comment_id, status='published')
        body = request.POST.get('body', '').strip()
        if len(body) < MIN_COMMENT_LENGTH:
            return render(request, 'blog/partials/comment_node.html', {
                'comment': parent, 'show_status': True, 'liked_comment_ids': set(),
            }, status=400)

        reply = PostComment.objects.create(post=parent.post, user=request.user, parent=parent, body=body, status='pending')
        return render(request, 'blog/partials/comment_node.html', {
            'comment': reply, 'show_status': True, 'liked_comment_ids': set(),
        })


class PostCommentLikeToggleView(LoginRequiredMixin, View):
    """ لایک/آنلایک یک نظر وبلاگ با یک کلیک (toggle) """

    def post(self, request, comment_id, *args, **kwargs):
        comment = get_object_or_404(PostComment, id=comment_id, status='published')
        like = PostCommentLike.objects.filter(user=request.user, comment=comment).first()
        if like:
            like.delete()
            liked_comment_ids = set()
        else:
            PostCommentLike.objects.create(user=request.user, comment=comment)
            liked_comment_ids = {comment.id}

        return render(request, 'blog/partials/like_button.html', {
            'comment': comment, 'liked_comment_ids': liked_comment_ids, 'like_count': comment.likes.count(),
        })
