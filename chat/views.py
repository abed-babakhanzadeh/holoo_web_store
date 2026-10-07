from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.http import Http404, JsonResponse
from django.views.decorators.http import require_GET
from django.views.decorators.vary import vary_on_cookie
from django.views.generic import TemplateView

from products.models import SiteSettings

from . import cache as chatcache
from . import conversations as conv
from .models import Conversation
from .services import public_config


@require_GET
@vary_on_cookie
def config_view(request):
    """
    GET /chat/config/ ← پیکربندی عمومی ویجت (فقط آنچه رابط کاربری لازم دارد).
    اگر کل چت خاموش است یا این نوع بازدیدکننده (مهمان/کاربر) مجاز نیست {"enabled": false}.
    """
    payload = public_config(SiteSettings.cached(), is_authenticated=request.user.is_authenticated)
    response = JsonResponse(payload, json_dumps_params={'ensure_ascii': False})
    response['Cache-Control'] = 'private, max-age=30'
    return response


# ------------------------------------------------------------------ صفحه‌ی «پشتیبانی» در پنل کاربری

CUSTOMER_STATUS = {
    'waiting_operator': ('در انتظار کارشناس', 'wait'), 'active': ('در جریان', 'live'), 'waiting_customer': ('منتظر پاسخ شما', 'you'),
    'offline': ('پیام ثبت شد؛ در انتظار پاسخ', 'wait'), 'closed': ('بسته', 'closed'),
}


class SupportView(LoginRequiredMixin, TemplateView):
    """
    «پشتیبانی» پنل کاربری: دو زبانه‌ی مستقل. «گفتگوها» سوابق چت خود کاربر؛ «تیکت‌ها» ماژول تیکت (هنوز راه‌اندازی نشده؛ همان
    وضعیت «به‌زودی» بخش قبلی). هیچ وابستگی دیتابیسی بین چت و تیکت نیست.
    """
    template_name = 'chat/support.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        tab = 'tickets' if self.request.GET.get('tab') == 'tickets' else 'chats'
        context.update({'active_nav': 'support', 'tab': tab})
        if tab == 'chats':
            rows = Conversation.objects.filter(user=self.request.user).order_by('-last_message_at', '-id')
            page = Paginator(rows, 15).get_page(self.request.GET.get('page'))
            for item in page:
                item.status_label, item.status_tone = CUSTOMER_STATUS.get(item.status, (item.status, 'wait'))
            context['page_obj'] = page
        return context


class SupportDetailView(LoginRequiredMixin, TemplateView):
    """ متن کامل یک گفتگوی خود کاربر (فقط‌خواندنی؛ یادداشت داخلی هرگز). گفتگوی دیگران = ۴۰۴ """
    template_name = 'chat/support_detail.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        conversation = Conversation.objects.filter(public_id=kwargs['public_id'], user=self.request.user).first()
        if conversation is None:
            raise Http404
        messages = list(conversation.messages.filter(is_internal_note=False).select_related('operator').prefetch_related('attachments')
                        .order_by('seq'))
        if conversation.unread_for_customer:
            conv.mark_read(conversation, 'customer', conversation.last_message_seq)
            chatcache.mark_customer_seen(conversation.pk)
        label, tone = CUSTOMER_STATUS.get(conversation.status, (conversation.status, 'wait'))
        context.update({'active_nav': 'support', 'conversation': conversation, 'messages_list': messages,
                        'status_label': label, 'status_tone': tone})
        return context
