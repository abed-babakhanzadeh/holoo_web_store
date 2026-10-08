"""
صفحه‌ی «درخواست خرید چکی» در پنل کاربری (فاز F2): فرم ثبت، وضعیت درخواست جاری، انصراف و سابقه‌ی درخواست‌ها، و نمایش امن مدارک
خودِ مشتری. تمام منطق در accounts/cheque_credit_service.py است؛ اینجا فقط HTTP.

  - درخواست در انتظار دارد ← فقط کارت وضعیت (+ انصراف)؛ فرم تکراری نمایش داده نمی‌شود (و سرور هم رد می‌کند).
  - واجد شرایط و بدون درخواست در انتظار ← فرم. وگرنه ← توضیح دلیل (مثلاً مجوز فعال است).
  - ثبت درخواست هیچ‌وقت سبد خرید را تغییر نمی‌دهد (از سفارش و سبد جداست).
  - POST ← Redirect-GET بعد از موفقیت؛ خطاهای فیلد همراه با مقادیر وارد‌شده دوباره نمایش داده می‌شوند (تصاویر باید دوباره انتخاب شوند).
  - IDOR: درخواست/مدرک دیگران ← ۴۰۴ یکنواخت.
"""
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View

from . import cheque_credit_service as service
from .cheque_credit import ChequeCreditDocument, ChequeCreditRequest

# نام فیلد فایل در فرم ← نوع مدرک
FILE_FIELDS = (
    ('doc_cheque_book', ChequeCreditDocument.KIND_CHEQUE_BOOK),
    ('doc_national_card', ChequeCreditDocument.KIND_NATIONAL_CARD),
    ('doc_business_license', ChequeCreditDocument.KIND_BUSINESS_LICENSE),
    ('doc_other', ChequeCreditDocument.KIND_OTHER),
)
TEXT_FIELDS = ('business_name', 'bank_name', 'account_holder', 'iban', 'requested_limit', 'monthly_turnover', 'description')
ACCEPT = 'image/jpeg,image/png,image/webp'


class ChequeCreditView(LoginRequiredMixin, View):
    template_name = 'accounts/cheque_credit.html'

    def _context(self, request, errors=None, values=None):
        user = request.user
        errors = dict(errors or {})
        errors['all'] = errors.pop('__all__', '')                  # قالب جنگو به متغیر با «_» اول دسترسی ندارد
        pending = service.pending_request(user)
        blocker = None if pending else service.eligibility_blocker(user)
        history = list(ChequeCreditRequest.objects.filter(user=user).prefetch_related('documents').order_by('-created_at', '-id')[:20])
        if values is None:
            values = {'business_name': user.business_name or '', 'account_holder': f'{user.first_name or ""} {user.last_name or ""}'.strip()}
        return {
            'active_nav': 'check_request', 'pending': pending, 'blocker': blocker, 'history': history,
            'national_code_problem': service.national_code_problem(user) if not pending and not blocker else None,
            'can_apply': not pending and not blocker, 'errors': errors, 'values': values,
            'max_docs': service.MAX_DOCS, 'max_doc_mb': service.MAX_DOC_MB, 'accept': ACCEPT,
            'has_permission': bool(user.can_purchase_with_check),
        }

    def get(self, request):
        return render(request, self.template_name, self._context(request))

    def post(self, request):
        files = [(kind, f) for field, kind in FILE_FIELDS for f in request.FILES.getlist(field)]
        data = {field: request.POST.get(field, '') for field in TEXT_FIELDS}
        try:
            service.submit_request(request.user, data, files)
        except service.ChequeCreditError as error:
            if error.status == 409:                                # شرایط عوض شده (درخواست در انتظار، مجوز فعال، ...)
                messages.error(request, error.message)
                return redirect('accounts:cheque_credit')
            return render(request, self.template_name, self._context(request, error.errors, data), status=error.status)
        messages.success(request, 'درخواست شما ثبت شد و در صف بررسی قرار گرفت؛ نتیجه را از همین صفحه پیگیری کنید. سبد خرید شما حفظ شده است.')
        return redirect('accounts:cheque_credit')


class ChequeCreditCancelView(LoginRequiredMixin, View):
    """ انصراف از درخواستِ در انتظار (فقط POST) """

    def post(self, request, request_id):
        credit_request = get_object_or_404(ChequeCreditRequest, public_id=request_id, user=request.user)
        try:
            service.cancel_request(credit_request, request.user)
        except service.ChequeCreditError as error:
            messages.error(request, error.message)
        else:
            messages.success(request, 'درخواست شما لغو شد.')
        return redirect('accounts:cheque_credit')


class ChequeCreditDocumentView(LoginRequiredMixin, View):
    """ تصویر مدرکِ خودِ مشتری (برای تأیید آنچه فرستاده)؛ مدارک پاک‌شده یا دیگران ← ۴۰۴ """

    def get(self, request, document_id):
        document = get_object_or_404(ChequeCreditDocument.objects.select_related('request'), public_id=document_id,
                                     request__user=request.user)
        response = service.document_response(document)
        if response is None:
            raise Http404
        return response
