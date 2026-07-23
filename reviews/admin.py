from django.contrib import admin
from django.utils.html import format_html, format_html_join
from .models import Review, ReviewPoint, ReviewImage


class ReviewPointInline(admin.TabularInline):
    model = ReviewPoint
    extra = 0


class ReviewImageInline(admin.TabularInline):
    """ فقط نمایش/حذف؛ عکس‌های نظر همیشه از طریق فرم ثبت نظر در سایت اضافه می‌شوند (به‌خاطر نام‌گذاری بر اساس slot) """
    model = ReviewImage
    extra = 0
    fields = ['image', 'image_preview']
    readonly_fields = ['image', 'image_preview']

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description='پیش‌نمایش')
    def image_preview(self, obj):
        if not obj.pk or not obj.image:
            return '—'
        return format_html('<img src="{}" style="height:80px;border-radius:6px;object-fit:cover;">', obj.image.url)


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ['id', 'product', 'user', 'rating', 'body_preview', 'images_preview', 'status', 'is_verified_purchase', 'parent', 'created_at']
    list_filter = ['status', 'rating', 'is_verified_purchase']
    search_fields = ['product__name', 'user__phone_number', 'title', 'body']
    raw_id_fields = ['product', 'user', 'parent']
    readonly_fields = ['created_at', 'updated_at']
    inlines = [ReviewPointInline, ReviewImageInline]
    actions = ['approve_reviews', 'reject_reviews']

    @admin.display(description='متن نظر')
    def body_preview(self, obj):
        text = obj.body or ''
        return text if len(text) <= 100 else text[:100] + '…'

    @admin.display(description='تصاویر')
    def images_preview(self, obj):
        images = list(obj.images.all()[:3])
        if not images:
            return '—'
        thumbs = format_html_join(
            '', '<img src="{}" style="height:50px;width:50px;object-fit:cover;border-radius:6px;margin-inline-end:4px;">',
            ((img.image.url,) for img in images),
        )
        return thumbs

    @admin.action(description='تایید نظرات/پاسخ‌های انتخاب‌شده')
    def approve_reviews(self, request, queryset):
        # با .save() تک‌تک (نه queryset.update) تا هوک بازمحاسبه‌ی «خریدار تایید‌شده» در Review.save() اجرا شود
        for review in queryset:
            review.status = 'published'
            review.rejection_reason = ''
            review.save()

    @admin.action(description='رد نظرات/پاسخ‌های انتخاب‌شده')
    def reject_reviews(self, request, queryset):
        for review in queryset:
            review.status = 'rejected'
            review.save()
