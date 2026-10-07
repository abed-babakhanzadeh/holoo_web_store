from django.http import JsonResponse
from django.views.decorators.http import require_GET
from django.views.decorators.vary import vary_on_cookie

from products.models import SiteSettings

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
