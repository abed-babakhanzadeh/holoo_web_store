from django.contrib import admin
from django.db import models
from django.utils import timezone
from .models import BlogCategory, Tag, BlogAuthor, Post, PostComment, PostCommentLike
from services.jalali_widgets import JalaliSplitDateTimeField


@admin.register(BlogCategory)
class BlogCategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('name',)
    prepopulated_fields = {'slug': ('name',)}


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ('name',)
    search_fields = ('name',)
    prepopulated_fields = {'slug': ('name',)}


@admin.register(BlogAuthor)
class BlogAuthorAdmin(admin.ModelAdmin):
    list_display = ('name', 'role', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('name',)


@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = ('title', 'category', 'author', 'status', 'published_at', 'views_count', 'read_time_minutes')
    list_filter = ('status', 'category', 'author')
    search_fields = ('title', 'excerpt')
    prepopulated_fields = {'slug': ('title',)}
    autocomplete_fields = ['category', 'author', 'tags']
    readonly_fields = ('views_count', 'read_time_minutes', 'created_at', 'updated_at')
    formfield_overrides = {
        models.DateTimeField: {'form_class': JalaliSplitDateTimeField},
    }

    def get_changeform_initial_data(self, request):
        # پیش‌فرض فیلد تاریخ انتشار روی تاریخ و ساعت جاری (برای فرم افزودن مقاله‌ی جدید)
        initial = super().get_changeform_initial_data(request)
        initial.setdefault('published_at', timezone.now())
        return initial

    fieldsets = (
        ('اطلاعات پایه', {
            'fields': ('title', 'slug', 'category', 'author', 'tags', 'cover_image', 'excerpt', 'status', 'published_at'),
        }),
        ('متن مقاله', {
            'fields': ('body',),
        }),
        ('آمار', {
            'fields': ('views_count', 'read_time_minutes', 'created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )


@admin.register(PostComment)
class PostCommentAdmin(admin.ModelAdmin):
    list_display = ('id', 'post', 'user', 'status', 'parent', 'created_at')
    list_filter = ('status',)
    search_fields = ('post__title', 'user__phone_number', 'body')
    raw_id_fields = ('post', 'user', 'parent')
    readonly_fields = ('created_at',)
    actions = ['approve_comments', 'reject_comments']

    @admin.action(description='تایید نظرات/پاسخ‌های انتخاب‌شده')
    def approve_comments(self, request, queryset):
        for comment in queryset:
            comment.status = 'published'
            comment.save()

    @admin.action(description='رد نظرات/پاسخ‌های انتخاب‌شده')
    def reject_comments(self, request, queryset):
        for comment in queryset:
            comment.status = 'rejected'
            comment.save()


@admin.register(PostCommentLike)
class PostCommentLikeAdmin(admin.ModelAdmin):
    list_display = ('comment', 'user', 'created_at')
    raw_id_fields = ('comment', 'user')
