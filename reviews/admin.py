from django.contrib import admin
from .models import Review, ReviewPoint, ReviewImage


class ReviewPointInline(admin.TabularInline):
    model = ReviewPoint
    extra = 0


class ReviewImageInline(admin.TabularInline):
    model = ReviewImage
    extra = 0


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ['id', 'product', 'user', 'rating', 'status', 'is_verified_purchase', 'parent', 'created_at']
    list_filter = ['status', 'rating', 'is_verified_purchase']
    search_fields = ['product__name', 'user__phone_number', 'title', 'body']
    raw_id_fields = ['product', 'user', 'parent']
    readonly_fields = ['created_at', 'updated_at']
    inlines = [ReviewPointInline, ReviewImageInline]
    actions = ['approve_reviews', 'reject_reviews']

    @admin.action(description='تایید نظرات انتخاب‌شده')
    def approve_reviews(self, request, queryset):
        queryset.filter(parent__isnull=True).update(status='published', rejection_reason='')

    @admin.action(description='رد نظرات انتخاب‌شده')
    def reject_reviews(self, request, queryset):
        queryset.filter(parent__isnull=True).update(status='rejected')
