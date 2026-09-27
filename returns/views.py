"""
ویزارد سه‌مرحله‌ای ثبت درخواست مرجوعی (Phase 1 - Part C.1).

هر سه مرحله فقط داده‌ی خام را در سشن جمع می‌کنند؛ تنها نقطه‌ای که رکورد واقعی ساخته می‌شود،
انتهای گام سوم با یک‌بار صدا زدن services.create_return_request است - هیچ منطق رزرو ظرفیت،
محاسبه‌ی ریفاند یا گذار وضعیت این‌جا تکرار نمی‌شود.

تمپلیت‌های این فاز عمداً کارکردی/اسکلت‌اند (بدون طراحی نهایی آرینو) - فقط برای تست جریان.
"""

import uuid

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.files.base import File
from django.core.files.storage import default_storage
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic import TemplateView

from orders.models import Order
from products.models import SiteSettings

from . import services
from .deadline import calculate_return_deadline, is_order_within_return_window
from .forms import ReturnStepOneForm, ReturnStepThreeForm, ReturnStepTwoForm
from .models import ReturnabilityRule, ReturnReason, ReturnRequest
from .refund_calculator import get_returnable_quantity

SESSION_KEY = 'return_wizard_data'


def _temp_attachment_path(upload_token, item_id, filename):
    """ مسیر موقتِ مدرکِ آپلودشده در گام ۲، پیش از وجود ReturnItem واقعی (Part C.3) """
    return f'returns/tmp_uploads/{upload_token}/{item_id}/{uuid.uuid4().hex}_{filename}'


def _delete_temp_attachments(data):
    """ پاک‌سازی همه‌ی فایل‌های موقتِ ویزارد جاری (بعد از ثبت موفق یا شکست نهایی) """
    for step2_info in data.get('step2', {}).values():
        for attachment in step2_info.get('attachments', []):
            default_storage.delete(attachment['temp_path'])


class ReturnProcedureView(TemplateView):
    """ رندر متن قوانین/رویه‌ی مرجوعی (SiteSettings.return_policy_html) + مهلت فعلی """
    template_name = 'returns/procedure.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        settings_obj = SiteSettings.cached()
        context['return_policy_html'] = settings_obj.return_policy_html
        context['return_period_days'] = settings_obj.return_period_days
        context['return_period_unit'] = settings_obj.return_period_unit
        return context


class ReturnWizardStepOneView(LoginRequiredMixin, View):
    """ انتخاب اقلام + تعداد هر قلم؛ گارد تحویل/مهلت همین‌جا (شروع ویزارد) اعمال می‌شود """
    template_name = 'returns/wizard_step1.html'

    def _get_order(self, request, order_id):
        return get_object_or_404(Order, pk=order_id, user=request.user)

    def _check_window(self, request, order):
        within, reason = is_order_within_return_window(order)
        if not within:
            messages.error(request, reason or 'امکان ثبت درخواست مرجوعی برای این سفارش وجود ندارد.')
            return False
        return True

    def _context(self, request, order, form):
        settings_obj = SiteSettings.cached()
        deadline = None
        if order.delivered_at:
            deadline = calculate_return_deadline(
                order.delivered_at, settings_obj.return_period_days, settings_obj.return_period_unit,
            )
        item_rows = []
        for item in form.order_items:
            blocking_rule = ReturnabilityRule.find_blocking_rule(item.product) if item.product_id else None
            item_rows.append({
                'item': item,
                'field_name': f'quantity_{item.pk}',
                'max_qty': form.returnable_quantities[item.pk],
                'blocking_rule': blocking_rule,
            })
        return {
            'order': order, 'form': form, 'item_rows': item_rows, 'deadline': deadline,
            'return_period_days': settings_obj.return_period_days, 'return_period_unit': settings_obj.return_period_unit,
            'active_nav': 'orders',
        }

    def get(self, request, order_id):
        order = self._get_order(request, order_id)
        if not self._check_window(request, order):
            return redirect('orders:order_detail_full', order_id=order.id)
        form = ReturnStepOneForm(order=order)
        return render(request, self.template_name, self._context(request, order, form))

    def post(self, request, order_id):
        order = self._get_order(request, order_id)
        if not self._check_window(request, order):
            return redirect('orders:order_detail_full', order_id=order.id)

        form = ReturnStepOneForm(request.POST, order=order)
        if not form.is_valid():
            return render(request, self.template_name, self._context(request, order, form))

        request.session[SESSION_KEY] = {
            'order_id': order.id,
            'step1': {str(pk): qty for pk, qty in form.selected_quantities().items()},
        }
        return redirect('returns:wizard_step2', order_id=order.id)


class ReturnWizardStepTwoView(LoginRequiredMixin, View):
    """ دلیل و توضیح برای هر قلم انتخاب‌شده در گام یک؛ بدون داده‌ی گام یک در سشن، به گام یک برمی‌گردد """
    template_name = 'returns/wizard_step2.html'

    def _load(self, request, order_id):
        order = get_object_or_404(Order, pk=order_id, user=request.user)
        data = request.session.get(SESSION_KEY)
        if not data or data.get('order_id') != order.id or not data.get('step1'):
            return order, None
        item_ids = [int(pk) for pk in data['step1'].keys()]
        order_items = list(order.items.filter(pk__in=item_ids).select_related('product'))
        if len(order_items) != len(item_ids):
            return order, None   # داده‌ی سشن با اقلام واقعی سفارش هم‌خوان نیست
        return order, order_items

    def _context(self, request, order, order_items, form):
        step1 = request.session[SESSION_KEY]['step1']
        item_rows = [{
            'item': item, 'quantity': step1[str(item.pk)],
            'reason_field': f'reason_{item.pk}', 'description_field': f'description_{item.pk}',
            'attachments_field': f'attachments_{item.pk}',
        } for item in order_items]
        requires_description_map = {
            str(r.pk): r.requires_description for r in ReturnReason.objects.filter(is_active=True)
        }
        return {
            'order': order, 'form': form, 'item_rows': item_rows,
            'requires_description_map': requires_description_map, 'active_nav': 'orders',
        }

    def get(self, request, order_id):
        order, order_items = self._load(request, order_id)
        if order_items is None:
            messages.error(request, 'ابتدا باید اقلام مرجوعی را در گام اول انتخاب کنید.')
            return redirect('returns:wizard_step1', order_id=order.id)
        form = ReturnStepTwoForm(order_items=order_items)
        return render(request, self.template_name, self._context(request, order, order_items, form))

    def post(self, request, order_id):
        order, order_items = self._load(request, order_id)
        if order_items is None:
            messages.error(request, 'ابتدا باید اقلام مرجوعی را در گام اول انتخاب کنید.')
            return redirect('returns:wizard_step1', order_id=order.id)

        form = ReturnStepTwoForm(request.POST, request.FILES, order_items=order_items)
        if not form.is_valid():
            return render(request, self.template_name, self._context(request, order, order_items, form))

        wizard_data = request.session[SESSION_KEY]
        # اگر کاربر قبلاً از این گام رد شده بود (مثلاً برگشت از گام ۳)، مدارک موقتِ قبلی همین
        # ویزارد یتیم نمانند - هر بار که گام ۲ دوباره ثبت می‌شود، مجموعه‌ی مدارک از نو ساخته می‌شود
        _delete_temp_attachments(wizard_data)
        upload_token = wizard_data.get('upload_token') or uuid.uuid4().hex

        reasons_and_descriptions = form.reasons_and_descriptions()
        attachments_by_item = form.attachments_by_item()
        step2_data = {}
        for item_id, info in reasons_and_descriptions.items():
            staged = []
            for attachment in attachments_by_item.get(item_id, []):
                path = _temp_attachment_path(upload_token, item_id, attachment['file'].name)
                saved_path = default_storage.save(path, attachment['file'])
                staged.append({
                    'temp_path': saved_path, 'attachment_type': attachment['attachment_type'],
                    'original_filename': attachment['original_filename'],
                })
            step2_data[str(item_id)] = {
                'reason_id': info['reason'].pk, 'description': info['description'], 'attachments': staged,
            }

        wizard_data['upload_token'] = upload_token
        wizard_data['step2'] = step2_data
        request.session[SESSION_KEY] = wizard_data
        return redirect('returns:wizard_step3', order_id=order.id)


class ReturnWizardStepThreeView(LoginRequiredMixin, View):
    """ روش بازپرداخت + ثبت نهایی. تنها نقطه‌ی صدا زدن services.create_return_request """
    template_name = 'returns/wizard_step3.html'

    def _load(self, request, order_id):
        order = get_object_or_404(Order, pk=order_id, user=request.user)
        data = request.session.get(SESSION_KEY)
        if not data or data.get('order_id') != order.id or not data.get('step1') or not data.get('step2'):
            return order, None
        return order, data

    def _review_rows(self, order, data):
        item_ids = [int(pk) for pk in data['step1'].keys()]
        order_items_by_id = {oi.pk: oi for oi in order.items.filter(pk__in=item_ids).select_related('product')}
        reason_ids = {int(info['reason_id']) for info in data['step2'].values()}
        reasons_by_id = {r.pk: r for r in ReturnReason.objects.filter(pk__in=reason_ids)}
        rows = []
        for pk_str, quantity in data['step1'].items():
            step2_info = data['step2'].get(pk_str, {})
            rows.append({
                'item': order_items_by_id[int(pk_str)], 'quantity': quantity,
                'reason': reasons_by_id.get(int(step2_info.get('reason_id', 0))),
                'description': step2_info.get('description', ''),
            })
        return rows

    def get(self, request, order_id):
        order, data = self._load(request, order_id)
        if data is None:
            messages.error(request, 'ابتدا باید مراحل قبلی ویزارد مرجوعی را کامل کنید.')
            return redirect('returns:wizard_step1', order_id=order.id)
        form = ReturnStepThreeForm(user=request.user)
        review_rows = self._review_rows(order, data)
        return render(request, self.template_name, {
            'order': order, 'form': form, 'review_rows': review_rows, 'active_nav': 'orders',
        })

    def post(self, request, order_id):
        order, data = self._load(request, order_id)
        if data is None:
            messages.error(request, 'ابتدا باید مراحل قبلی ویزارد مرجوعی را کامل کنید.')
            return redirect('returns:wizard_step1', order_id=order.id)

        form = ReturnStepThreeForm(request.POST, user=request.user)
        if not form.is_valid():
            review_rows = self._review_rows(order, data)
            return render(request, self.template_name, {
                'order': order, 'form': form, 'review_rows': review_rows, 'active_nav': 'orders',
            })

        items, opened_files = self._build_items(order, data)
        refund_method = form.cleaned_data['refund_method']
        bank_account = form.resolve_bank_account()

        try:
            try:
                return_request = services.create_return_request(
                    order, request.user, items, refund_method=refund_method, bank_account=bank_account,
                )
            finally:
                # ویندوز فایلِ منبع را تا وقتی بسته نشود قفل نگه می‌دارد؛ اگر همین‌جا نبندیم،
                # حذف موقتی‌ها (زیر) با PermissionError شکست می‌خورد - مستقل از موفقیت/شکست بالا
                for opened_file in opened_files:
                    opened_file.close()
        except (services.OrderNotDeliveredError, services.ReturnWindowExpiredError) as exc:
            messages.error(request, str(exc))
            _delete_temp_attachments(data)
            request.session.pop(SESSION_KEY, None)
            return redirect('orders:order_detail_full', order_id=order.id)
        except services.InsufficientReturnableQuantityError as exc:
            messages.error(request, str(exc))
            _delete_temp_attachments(data)
            request.session.pop(SESSION_KEY, None)
            return redirect('returns:wizard_step1', order_id=order.id)
        except ValueError as exc:
            # مبلغ/حساب نامعتبر: مدارکِ موقت دست‌نخورده می‌مانند تا کاربر همین گام ۳ را دوباره
            # امتحان کند، بدون اینکه مجبور شود دوباره فایل‌ها را آپلود کند
            messages.error(request, str(exc))
            return redirect('returns:wizard_step3', order_id=order.id)

        # فایل‌های موقت پس از موفقیت کپی/متصل شده‌اند (services.create_return_request)؛ نسخه‌ی
        # موقتِ روی دیسک دیگر لازم نیست
        _delete_temp_attachments(data)
        request.session.pop(SESSION_KEY, None)
        return redirect('returns:wizard_success', pk=return_request.pk)

    @staticmethod
    def _build_items(order, data):
        """ برمی‌گرداند: (items برای create_return_request, فهرست فایل‌های بازشده‌ای که باید بسته شوند) """
        item_ids = [int(pk) for pk in data['step1'].keys()]
        order_items_by_id = {oi.pk: oi for oi in order.items.filter(pk__in=item_ids)}
        reason_ids = {int(info['reason_id']) for info in data['step2'].values()}
        reasons_by_id = {r.pk: r for r in ReturnReason.objects.filter(pk__in=reason_ids)}

        items = []
        opened_files = []
        for pk_str, quantity in data['step1'].items():
            item_id = int(pk_str)
            step2_info = data['step2'].get(pk_str, {})
            attachments = []
            for attachment in step2_info.get('attachments', []):
                opened = default_storage.open(attachment['temp_path'])
                opened_files.append(opened)
                attachments.append({
                    'file': File(opened, name=attachment['original_filename']),
                    'attachment_type': attachment['attachment_type'],
                    'original_filename': attachment['original_filename'],
                })
            items.append({
                'order_item': order_items_by_id[item_id],
                'reason': reasons_by_id[int(step2_info['reason_id'])],
                'requested_quantity': quantity,
                'description': step2_info.get('description', ''),
                'attachments': attachments,
            })
        return items, opened_files


class ReturnSuccessView(LoginRequiredMixin, TemplateView):
    template_name = 'returns/wizard_success.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['return_request'] = get_object_or_404(ReturnRequest, pk=self.kwargs['pk'], user=self.request.user)
        context['active_nav'] = 'orders'
        return context
