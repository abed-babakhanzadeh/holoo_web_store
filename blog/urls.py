from django.urls import path
from . import views

app_name = 'blog'
urlpatterns = [
    # مسیرهای اکشن نظرات باید قبل از مسیر عمومی <str:slug>/ تعریف شوند تا با اسلاگ مقاله تداخل نکنند
    path('comment/<str:post_slug>/create/', views.PostCommentCreateView.as_view(), name='comment_create'),
    path('comment/<int:comment_id>/reply/', views.PostCommentReplyView.as_view(), name='comment_reply'),
    path('comment/<int:comment_id>/like/', views.PostCommentLikeToggleView.as_view(), name='comment_like'),

    path('', views.PostListView.as_view(), name='list'),
    path('<str:slug>/', views.PostDetailView.as_view(), name='detail'),
]
