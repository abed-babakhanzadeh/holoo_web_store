from django.urls import path
from . import views

app_name = 'reviews'
urlpatterns = [
    path('product/<str:product_slug>/create/', views.ReviewCreateView.as_view(), name='create'),
    path('<int:review_id>/reply/', views.ReviewReplyView.as_view(), name='reply'),
    path('<int:review_id>/reply-edit/', views.ReviewReplyEditView.as_view(), name='reply_edit'),
    path('<int:review_id>/edit/', views.ReviewEditView.as_view(), name='edit'),
    path('<int:review_id>/delete/', views.ReviewDeleteView.as_view(), name='delete'),
    path('mine/', views.MyReviewListView.as_view(), name='my_reviews'),
]
