from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db.models import Exists, OuterRef, Q
from django.http import Http404, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join

from payments.models import Transaction
from products.pricing import CHECK as PRICING_CHECK

from .approval import ApprovalError, approve_order
from . import cheques as cheque_service
from .approval import approval_blocker
from .cheques import ChequeError, image_response
from .models import ChequeImage, ChequePayment, Order, OrderItem


class ReviewFilter(admin.SimpleListFilter):
    """ «در انتظار تأیید مدیر»: ثبت‌شده، تأییدنشده، و پرداخت‌شده (آنلاین/کیف‌پول) یا چکی """
    title = 'تأیید مدیر'
    parameter_name = 'review'

    def lookups(self, request, model_admin):
        return (('pending', 'در انتظار تأیید مدیر'), ('approved', 'تأییدشده'))

    def queryset(self, request, queryset):
        if self.value() == 'pending':
            paid = Exists(Transaction.objects.filter(order=OuterRef('pk'), status='success'))
            return queryset.filter(status='pending', approved_at__isnull=True).annotate(_paid=paid).filter(
                Q(_paid=True) | Q(settlement=Order.SETTLEMENT_CHEQUE) | Q(payment_method=PRICING_CHECK))
        if self.value() == 'approved':
            return queryset.filter(approved_at__isnull=False)
        return queryset

class OrderItemInline(admin.TabularInline):
    model = OrderItem
    raw_id_fields = ['product']
    extra = 0
    # اسنپ‌شات تخفیف لحظه‌ی ثبت است و با فاکتور هلو هماهنگ؛ ویرایش دستی‌اش مغایرت مالی می‌سازد
    readonly_fields = ['original_price', 'discount_amount']

class ChequePaymentInline(admin.TabularInline):
    """ چک‌های ثبت‌شده‌ی سفارش (فقط‌خواندنی). بررسی (تأیید/رد با علت) در صفحه‌ی خودِ چک انجام می‌شود (لینک «بررسی»). """
    model = ChequePayment
    extra = 0
    can_delete = False
    fields = ['sayadi_id', 'amount', 'due_date', 'bank_name', 'holder_name', 'status', 'rejection_reason', 'images_preview', 'review_link']
    readonly_fields = fields

    @admin.display(description='تصاویر')
    def images_preview(self, obj):
        return cheque_images_html(obj)

    @admin.display(description='بررسی')
    def review_link(self, obj):
        if not obj.pk:
            return '—'
        return format_html('<a href="{}">بررسی و تأیید/رد</a>', reverse('admin:orders_chequepayment_change', args=[obj.pk]))

    def has_add_permission(self, request, obj=None):
        return False


def cheque_images_html(cheque):
    """ بندانگشتی‌ها (از ویوی دارای کنترل دسترسی ادمین)، هر کدام لینک به تصویر کامل """
    if not cheque.pk:
        return '—'
    items = []
    for image in cheque.images.all():
        url = reverse('admin:orders_chequepayment_image', args=[image.public_id])
        items.append(format_html('<a href="{0}" target="_blank" rel="noopener"><img src="{0}" alt="" style="height:70px;margin:2px;border-radius:4px"></a>', url))
    return format_html_join('', '{}', ((item,) for item in items)) if items else '—'


class ChequeReviewForm(forms.ModelForm):
    """ فرم بررسی چک: فقط وضعیت و علت رد؛ علت برای «ردشده» الزامی است (cheque_service.set_review همین قاعده را سمت سرور اجرا می‌کند) """

    class Meta:
        model = ChequePayment
        fields = ['status', 'rejection_reason']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'status' in self.fields:
            self.fields['status'].choices = [c for c in ChequePayment.STATUS_CHOICES if c[0] != ChequePayment.STATUS_WITHDRAWN]
        if 'rejection_reason' in self.fields:
            self.fields['rejection_reason'].widget = forms.Textarea(attrs={'rows': 3, 'cols': 70})
            self.fields['rejection_reason'].help_text = 'فقط برای رد چک؛ همین متن به مشتری نمایش داده می‌شود تا چک را اصلاح کند.'

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('status') == ChequePayment.STATUS_REJECTED and not (cleaned.get('rejection_reason') or '').strip():
            self.add_error('rejection_reason', 'برای رد چک، علت رد را بنویسید.')
        return cleaned


@admin.register(ChequePayment)
class ChequePaymentAdmin(admin.ModelAdmin):
    """
    بررسی چک‌های ثبت‌شده (فاز C): تأیید یا ردِ چک با علت الزامی (فرم همین صفحه یا اکشن‌های فهرست). رد، وضعیت سفارش را برای مشتری
    «نیاز به اصلاح چک» می‌کند و تا تأیید همه‌ی چک‌ها «تأیید سفارش» ممکن نیست (orders/approval.py). اطلاعات خودِ چک فقط‌خواندنی است.
    تصاویر فقط از همین ادمین (با مجوز مشاهده) سرو می‌شوند. افزودن/حذف ممکن نیست.
    """
    form = ChequeReviewForm
    list_display = ['id', 'order_link', 'sayadi_id', 'amount', 'due_date', 'bank_name', 'status', 'reviewed_by', 'created_at']
    list_filter = ['status', 'created_at']
    search_fields = ['sayadi_id', 'order__id', 'holder_name', 'bank_name']
    actions = ['approve_cheques', 'reject_cheques']
    fields = ['order', 'sayadi_id', 'amount', 'due_date', 'bank_name', 'holder_name', 'images_preview', 'status', 'rejection_reason',
              'reviewed_by', 'reviewed_at', 'created_at', 'updated_at']
    static_readonly = ['order', 'sayadi_id', 'amount', 'due_date', 'bank_name', 'holder_name', 'images_preview', 'reviewed_by',
                       'reviewed_at', 'created_at', 'updated_at']

    def get_readonly_fields(self, request, obj=None):
        readonly = list(self.static_readonly)
        if obj is not None and (obj.status == ChequePayment.STATUS_WITHDRAWN or cheque_service._order_locked_reason(obj.order)):
            readonly += ['status', 'rejection_reason']                       # چک حذف‌شده یا سفارش تأییدشده/لغوشده: فقط مشاهده
        return readonly

    def save_model(self, request, obj, form, change):
        if 'status' not in form.cleaned_data:                    # چک حذف‌شده/سفارش قفل‌شده: فیلدها فقط‌خواندنی‌اند
            return
        try:
            cheque_service.set_review(obj, form.cleaned_data['status'], request.user, form.cleaned_data.get('rejection_reason'))
        except ChequeError as error:
            self.message_user(request, ' '.join(str(v) for v in error.errors.values()), level=messages.ERROR)

    @admin.action(description='تأیید چک‌های انتخاب‌شده')
    def approve_cheques(self, request, queryset):
        if not self.has_change_permission(request):
            self.message_user(request, 'اجازه‌ی بررسی چک را ندارید.', level=messages.ERROR)
            return
        done = 0
        for cheque in queryset.select_related('order'):
            try:
                cheque_service.set_review(cheque, ChequePayment.STATUS_APPROVED, request.user)
                done += 1
            except ChequeError as error:
                self.message_user(request, f'چک {cheque.sayadi_id}: ' + ' '.join(str(v) for v in error.errors.values()), level=messages.WARNING)
        if done:
            self.message_user(request, f'{done} چک تأیید شد.', level=messages.SUCCESS)

    @admin.action(description='رد چک‌های انتخاب‌شده (با علت)')
    def reject_cheques(self, request, queryset):
        """ صفحه‌ی میانی: علت رد الزامی است و برای همه‌ی چک‌های انتخاب‌شده ثبت می‌شود """
        if not self.has_change_permission(request):
            self.message_user(request, 'اجازه‌ی بررسی چک را ندارید.', level=messages.ERROR)
            return None
        reason = (request.POST.get('reason') or '').strip()
        if request.POST.get('apply') and reason:
            done = 0
            for cheque in queryset.select_related('order'):
                try:
                    cheque_service.set_review(cheque, ChequePayment.STATUS_REJECTED, request.user, reason)
                    done += 1
                except ChequeError as error:
                    self.message_user(request, f'چک {cheque.sayadi_id}: ' + ' '.join(str(v) for v in error.errors.values()),
                                      level=messages.WARNING)
            if done:
                self.message_user(request, f'{done} چک رد شد و علت به مشتری نمایش داده می‌شود.', level=messages.SUCCESS)
            return None
        context = {
            **self.admin_site.each_context(request), 'opts': self.model._meta, 'title': 'رد چک‌های انتخاب‌شده',
            'cheques': queryset, 'action_name': 'reject_cheques', 'reason': reason,
            'error': 'علت رد را بنویسید.' if request.POST.get('apply') else '',
            'select_across': request.POST.get('select_across', '0'),
            'selected': request.POST.getlist(admin.helpers.ACTION_CHECKBOX_NAME),
        }
        return render(request, 'admin/orders/chequepayment/reject_reason.html', context)

    @admin.display(description='سفارش')
    def order_link(self, obj):
        return format_html('<a href="{}">#{}</a>', reverse('admin:orders_order_change', args=[obj.order_id]), obj.order_id)

    @admin.display(description='تصاویر چک')
    def images_preview(self, obj):
        return cheque_images_html(obj)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('order').prefetch_related('images')

    def get_urls(self):
        custom = [path('image/<uuid:image_id>/', self.admin_site.admin_view(self.image_view), name='orders_chequepayment_image')]
        return custom + super().get_urls()

    def image_view(self, request, image_id):
        if not self.has_view_permission(request):
            raise PermissionDenied
        image = ChequeImage.objects.filter(public_id=image_id).first()
        response = image_response(image) if image else None
        if response is None:
            raise Http404
        return response

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'first_name', 'phone', 'city', 'shipping_method', 'payment_method', 'settlement', 'cheque_state_display', 'total_price', 'status', 'approved_at', 'tracking_code', 'is_paid', 'holoo_invoice_id', 'holoo_sync_alert_sent', 'created_at']
    list_filter = [ReviewFilter, 'status', 'payment_method', 'settlement', 'shipping_method', 'holoo_needs_attention', 'holoo_sync_alert_sent', 'created_at']
    search_fields = ['first_name', 'last_name', 'phone', 'holoo_invoice_id', 'city', 'province', 'coupon_code']
    inlines = [OrderItemInline, ChequePaymentInline]
    actions = ['approve_orders', 'retry_holoo_registration']
    change_form_template = 'admin/orders/order/change_form.html'

    # اسنپ‌شات مقصد و روش ارسال در لحظه‌ی ثبت سفارش گرفته می‌شود و همان فاکتورِ ثبت‌شده است (در هلو هم همین رفته)؛
    # اپراتور نباید تاریخچه‌ی آن را دستکاری کند. اصلاح تایپیِ خودِ متن آدرس/گیرنده با فیلدهای عادی ممکن است.
    # مبلغ‌ها (کرایه و جمع کل) هم فقط‌خواندنی‌اند: با تراکنش بانکی و فاکتور هلو هماهنگ‌اند و تغییر دستی‌شان
    # مغایرت مالی می‌سازد.
    readonly_fields = ['settlement', 'created_at', 'updated_at', 'canceled_at', 'approved_at', 'approved_by',
                       'holoo_invoice_erp_code', 'holoo_needs_attention', 'holoo_last_error', 'province', 'city', 'zone', 'full_address_display',
                       'shipping_method', 'shipping_label', 'shipping_cost', 'total_price',
                       'promotion_discount', 'order_discount', 'order_discount_label', 'coupon_code', 'shipping_discount']

    fieldsets = (
        (None, {'fields': ('user', 'status', 'cancel_reason', 'tracking_code', 'payment_method', 'settlement', 'total_price', 'shipping_cost')}),
        ('تخفیف (اسنپ‌شات لحظه‌ی ثبت؛ غیرقابل ویرایش)', {
            'fields': ('promotion_discount', 'order_discount', 'order_discount_label', 'coupon_code', 'shipping_discount'),
        }),
        ('گیرنده', {'fields': ('first_name', 'last_name', 'phone', 'postal_code', 'address')}),
        ('مقصد و روش ارسال (اسنپ‌شات لحظه‌ی ثبت؛ غیرقابل ویرایش)', {
            'fields': ('province', 'city', 'zone', 'full_address_display', 'shipping_method', 'shipping_label'),
        }),
        ('تأیید مدیر', {
            'fields': ('approved_at', 'approved_by'),
            'description': 'سفارش پرداخت‌شده یا چکی «در انتظار تأیید مدیر» می‌ماند و موجودی‌اش در سایت رزرو است. فاکتور قطعی هلو '
                           'فقط پس از «تأیید سفارش» (دکمه‌ی بالای همین صفحه یا اکشن لیست) صادر می‌شود.',
        }),
        ('حسابداری هلو', {'fields': ('holoo_invoice_id', 'holoo_invoice_erp_code', 'holoo_receipt_id', 'holoo_needs_attention',
                                      'holoo_last_error', 'holoo_sync_alert_sent')}),
        ('زمان‌ها', {'fields': ('created_at', 'updated_at', 'canceled_at')}),
    )

    def get_urls(self):
        custom = [
            path('<int:object_id>/approve/', self.admin_site.admin_view(self.approve_view), name='orders_order_approve'),
        ]
        return custom + super().get_urls()

    @staticmethod
    def _holoo_write_warning():
        """
        متن هشدار وقتی نوشتن در هلو «واقعی» نیست (غیرفعال یا شبیه‌سازی)؛ در این حالت تأیید سفارش فاکتور واقعی نمی‌سازد
        (mock: فقط یک شماره‌ی INV_ ساختگی روی سفارش می‌نشیند). None وقتی واقعی است.
        """
        from holoo.conf import get_config
        config = get_config()
        if config.write_is_real:
            return None
        if config.write_is_mock:
            return ('اتصال هلو روی حالت شبیه‌سازی (HOLOO_WRITE_MODE=mock) است؛ فاکتورِ سفارش‌های تأییدشده در هلو ثبت نمی‌شود '
                    '(فقط شماره‌ی INV_ ساختگی روی سفارش می‌نشیند). برای ثبت واقعی در فایل .env سرور HOLOO_WRITE_MODE=real بگذارید.')
        return ('نوشتن در هلو غیرفعال است (HOLOO_WRITE_MODE=disabled یا دیتابیس هلو در HOLOO_WRITE_ALLOWED_DBS نیست)؛ '
                'سفارش‌های تأییدشده تا فعال شدن در صف می‌مانند.')

    def changelist_view(self, request, extra_context=None):
        warning = self._holoo_write_warning()
        if warning and request.method == 'GET':
            messages.warning(request, warning)
        return super().changelist_view(request, extra_context)

    @admin.display(description='وضعیت چک')
    def cheque_state_display(self, obj):
        if not obj.is_cheque:
            return '—'
        return {'missing': 'ثبت نشده', 'needs_correction': 'نیاز به اصلاح', 'under_review': 'در انتظار بررسی', 'approved': 'تأییدشده'}[obj.cheque_state]

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related('cheques')

    def change_view(self, request, object_id, form_url='', extra_context=None):
        extra_context = dict(extra_context or {})
        order = Order.objects.filter(pk=object_id).first() if object_id and str(object_id).isdigit() else None
        if order is not None and not order.approved_at and order.status == 'pending':
            extra_context['approval_blocker'] = approval_blocker(order)       # دلیل مسدود بودن «تأیید سفارش» (مثلاً چک تأییدنشده)
        return super().change_view(request, object_id, form_url, extra_context)

    def _approve_one(self, request, order):
        try:
            approve_order(order, by=request.user)
        except ApprovalError as error:
            messages.warning(request, f'سفارش #{order.pk} تأیید نشد: {error}')
            return False
        warning = self._holoo_write_warning()
        if warning:
            messages.warning(request, f'سفارش #{order.pk} تأیید شد، اما {warning}')
        else:
            messages.success(request, f'سفارش #{order.pk} تأیید شد و ثبت فاکتور در حسابداری در صف قرار گرفت.')
        return True

    def approve_view(self, request, object_id):
        """ دکمه‌ی «تأیید سفارش» صفحه‌ی ویرایش (فقط POST) """
        if request.method != 'POST':
            return HttpResponseNotAllowed(['POST'])
        order = get_object_or_404(Order, pk=object_id)
        if not self.has_change_permission(request, order):
            self.message_user(request, 'اجازه‌ی تأیید سفارش را ندارید.', level=messages.ERROR)
        else:
            self._approve_one(request, order)
        return redirect(reverse('admin:orders_order_change', args=[order.pk]))

    @admin.action(description='تأیید سفارش‌های انتخاب‌شده (صدور فاکتور در هلو)')
    def approve_orders(self, request, queryset):
        if not self.has_change_permission(request):
            self.message_user(request, 'اجازه‌ی تأیید سفارش را ندارید.', level=messages.ERROR)
            return
        for order in queryset.order_by('pk'):
            self._approve_one(request, order)

    @admin.action(description='ثبت مجدد فاکتور در هلو (پس از اصلاح خطا)')
    def retry_holoo_registration(self, request, queryset):
        """
        سفارش‌های تأییدشده‌ای که فاکتورشان هنوز در هلو ثبت نشده (خطای دائمی یا گیرکرده) دوباره به صف می‌روند. علامت خطا پاک
        می‌شود؛ اگر باز رد شود دوباره علامت می‌خورد. سفارش تأییدنشده/لغوشده/دارای فاکتور رد می‌شود.
        """
        from holoo.tasks import send_order_to_holoo
        queued = skipped = 0
        for order in queryset:
            if order.approved_at and not order.holoo_invoice_id and order.status not in ('canceled', 'rejected_stock'):
                Order.objects.filter(pk=order.pk).update(holoo_needs_attention=False, holoo_last_error='')
                send_order_to_holoo.delay(order.pk)
                queued += 1
            else:
                skipped += 1
        if queued:
            messages.success(request, f'{queued} سفارش دوباره برای ثبت در هلو به صف رفت.')
        if skipped:
            messages.warning(request, f'{skipped} سفارش رد شد (تأییدنشده، لغو/ردشده یا دارای فاکتور).')

    @admin.display(description='آدرس کامل')
    def full_address_display(self, obj):
        return obj.full_address or '-'

    def save_model(self, request, obj, form, change):
        """
        وقتی ادمین در همین ذخیره «وضعیت» یا «کد رهگیری» را عوض کند و بعد از ذخیره سفارش
        هم status='shipped' باشد هم tracking_code پر باشد، پیامک کد رهگیری برای مشتری
        می‌رود. شرط «در همین ذخیره تغییر کرده» (form.changed_data) از ارسال تکراری در
        ذخیره‌های بعدی که به این دو فیلد کاری ندارند جلوگیری می‌کند.
        """
        super().save_model(request, obj, form, change)

        shipped_now = obj.status == 'shipped' and obj.tracking_code and obj.user and (
            'status' in form.changed_data or 'tracking_code' in form.changed_data
        )
        if shipped_now:
            from notifications.service import notify
            notify(
                obj.user.phone_number, 'order_shipped_customer',
                name=obj.user.first_name or '', tracking_code=obj.tracking_code,
            )
