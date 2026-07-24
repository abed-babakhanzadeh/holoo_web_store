import math

from django.db import models
from django.utils import timezone
from django.utils.html import strip_tags
from django_ckeditor_5.fields import CKEditor5Field

from accounts.models import CustomUser

WORDS_PER_MINUTE = 200


def estimate_read_time(html_body):
    """ برآورد زمان مطالعه (دقیقه) از روی تعداد کلمات متن مقاله (بدون تگ‌های HTML) """
    text = strip_tags(html_body or '')
    word_count = len(text.split())
    return max(1, math.ceil(word_count / WORDS_PER_MINUTE))


def post_cover_upload_path(instance, filename):
    """ نام‌گذاری بر اساس اسلاگ مقاله (نه id، چون در اولین ذخیره هنوز pk موجود نیست) """
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'jpg'
    return f'blog/covers/{instance.slug}.{ext}'


class BlogCategory(models.Model):
    """ دسته‌بندی مطالب وبلاگ؛ کاملاً مستقل از دسته‌بندی محصولات (products.Category) """
    name = models.CharField(max_length=150, unique=True, verbose_name='نام دسته')
    slug = models.SlugField(max_length=150, unique=True, allow_unicode=True, verbose_name='اسلاگ')
    is_active = models.BooleanField(default=True, verbose_name='فعال')

    class Meta:
        verbose_name = 'دسته‌بندی وبلاگ'
        verbose_name_plural = 'دسته‌بندی‌های وبلاگ'
        ordering = ('name',)

    def __str__(self):
        return self.name


class Tag(models.Model):
    """ برچسب مقالات وبلاگ """
    name = models.CharField(max_length=100, unique=True, verbose_name='نام برچسب')
    slug = models.SlugField(max_length=100, unique=True, allow_unicode=True, verbose_name='اسلاگ')

    class Meta:
        verbose_name = 'برچسب'
        verbose_name_plural = 'برچسب‌ها'
        ordering = ('name',)

    def __str__(self):
        return self.name


class BlogAuthor(models.Model):
    """ نویسنده‌ی مطالب وبلاگ؛ عمداً به حساب کاربری سایت (CustomUser) وصل نیست چون نویسنده‌ی محتوا لزوماً مشتری سایت نیست """
    name = models.CharField(max_length=150, verbose_name='نام نویسنده')
    avatar = models.ImageField(upload_to='blog/authors/', blank=True, null=True, verbose_name='تصویر')
    role = models.CharField(max_length=150, blank=True, verbose_name='سمت/تخصص')
    bio = models.TextField(blank=True, verbose_name='بیوگرافی')
    instagram_url = models.URLField(blank=True, verbose_name='لینک اینستاگرام')
    twitter_url = models.URLField(blank=True, verbose_name='لینک توییتر/ایکس')
    linkedin_url = models.URLField(blank=True, verbose_name='لینک لینکدین')
    is_active = models.BooleanField(default=True, verbose_name='فعال')

    class Meta:
        verbose_name = 'نویسنده وبلاگ'
        verbose_name_plural = 'نویسندگان وبلاگ'
        ordering = ('name',)

    def __str__(self):
        return self.name


class VisiblePostManager(models.Manager):
    """ فقط مقالاتی که منتشر شده‌اند و تاریخ انتشارشان رسیده (کپی الگوی Product.visible) """

    def get_queryset(self):
        return super().get_queryset().filter(status='published', published_at__lte=timezone.now())


class Post(models.Model):
    STATUS_CHOICES = (
        ('draft', 'پیش‌نویس'),
        ('published', 'منتشر شده'),
    )

    title = models.CharField(max_length=255, verbose_name='عنوان')
    slug = models.SlugField(max_length=255, unique=True, allow_unicode=True, verbose_name='اسلاگ')
    category = models.ForeignKey(BlogCategory, related_name='posts', on_delete=models.SET_NULL, null=True, blank=True, verbose_name='دسته‌بندی')
    author = models.ForeignKey(BlogAuthor, related_name='posts', on_delete=models.SET_NULL, null=True, blank=True, verbose_name='نویسنده')
    tags = models.ManyToManyField(Tag, related_name='posts', blank=True, verbose_name='برچسب‌ها')

    cover_image = models.ImageField(upload_to=post_cover_upload_path, blank=True, null=True, verbose_name='تصویر شاخص')
    excerpt = models.CharField(max_length=300, blank=True, verbose_name='خلاصه کوتاه')
    body = CKEditor5Field('متن مقاله', blank=True, config_name='default')

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='draft', verbose_name='وضعیت')
    published_at = models.DateTimeField(null=True, blank=True, verbose_name='تاریخ انتشار')

    views_count = models.PositiveIntegerField(default=0, verbose_name='تعداد بازدید')
    read_time_minutes = models.PositiveSmallIntegerField(default=1, editable=False, verbose_name='زمان مطالعه (دقیقه)')

    created_at = models.DateTimeField(auto_now_add=True, verbose_name='تاریخ ثبت')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='آخرین ویرایش')

    objects = models.Manager()
    # مقالات قابل‌نمایش در سایت (منتشرشده + تاریخ انتشار رسیده)
    visible = VisiblePostManager()

    class Meta:
        verbose_name = 'مقاله وبلاگ'
        verbose_name_plural = 'مقالات وبلاگ'
        ordering = ('-published_at', '-created_at')

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if self.status == 'published' and not self.published_at:
            self.published_at = timezone.now()
        self.read_time_minutes = estimate_read_time(self.body)
        super().save(*args, **kwargs)


class PostComment(models.Model):
    """
    نظر یا پاسخ روی یک مقاله (کپی الگوی ساده‌شده‌ی reviews.Review، بدون امتیاز/عکس چون طرح
    استاتیک وبلاگ فقط یک فرم متنی دارد). بر خلاف Review، محدودیت «یک نظر برای هر کاربر» ندارد.
    """
    STATUS_CHOICES = (
        ('pending', 'در انتظار تایید'),
        ('published', 'تایید شده'),
        ('rejected', 'رد شده'),
    )

    post = models.ForeignKey(Post, related_name='comments', on_delete=models.CASCADE, verbose_name='مقاله')
    user = models.ForeignKey(CustomUser, related_name='blog_comments', on_delete=models.CASCADE, verbose_name='کاربر')
    parent = models.ForeignKey('self', null=True, blank=True, related_name='replies', on_delete=models.CASCADE, verbose_name='پاسخ به')

    body = models.TextField(verbose_name='متن')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending', verbose_name='وضعیت')

    created_at = models.DateTimeField(auto_now_add=True, verbose_name='تاریخ ثبت')

    class Meta:
        verbose_name = 'نظر وبلاگ'
        verbose_name_plural = 'نظرات وبلاگ'
        ordering = ('-created_at',)

    def __str__(self):
        if self.parent_id:
            return f"پاسخ {self.user} روی نظر #{self.parent_id}"
        return f"نظر {self.user} برای {self.post}"

    @property
    def is_reply(self):
        return self.parent_id is not None


class PostCommentLike(models.Model):
    """ لایک یک نظر وبلاگ توسط کاربر (کپی الگوی FavoriteProduct در wishlist) """
    user = models.ForeignKey(CustomUser, related_name='comment_likes', on_delete=models.CASCADE, verbose_name='کاربر')
    comment = models.ForeignKey(PostComment, related_name='likes', on_delete=models.CASCADE, verbose_name='نظر')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='تاریخ ثبت')

    class Meta:
        verbose_name = 'لایک نظر وبلاگ'
        verbose_name_plural = 'لایک‌های نظرات وبلاگ'
        unique_together = ('user', 'comment')
        ordering = ('-created_at',)

    def __str__(self):
        return f"{self.user} لایک کرد نظر #{self.comment_id}"
