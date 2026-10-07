from django.contrib import admin, messages
from django.db.models import Exists, OuterRef, Q
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect
from django.urls import path, reverse

from payments.models import Transaction
from products.pricing import CHECK as PRICING_CHECK

from .approval import ApprovalError, approve_order
from .models import Order, OrderItem


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

@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'first_name', 'phone', 'city', 'shipping_method', 'payment_method', 'settlement', 'total_price', 'status', 'approved_at', 'tracking_code', 'is_paid', 'holoo_invoice_id', 'holoo_sync_alert_sent', 'created_at']
    list_filter = [ReviewFilter, 'status', 'payment_method', 'settlement', 'shipping_method', 'holoo_needs_attention', 'holoo_sync_alert_sent', 'created_at']
    search_fields = ['first_name', 'last_name', 'phone', 'holoo_invoice_id', 'city', 'province', 'coupon_code']
    inlines = [OrderItemInline]
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
