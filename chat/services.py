"""
پیکربندی عمومی ویجت گفتگو برای کلاینت (GET /chat/config/).

دو بخش:
  - snapshot ایستا: از تنظیمات SiteSettings ساخته و ۵ دقیقه در کش می‌ماند (و با هر ذخیره‌ی تنظیمات باطل می‌شود).
  - بخش پویا: وضعیت ساعت کاری/دسترسی که با زمان عوض می‌شود و هر درخواست تازه محاسبه می‌شود (خالص و ارزان).
کش فقط شتاب‌دهنده است؛ خرابی Redis پیکربندی را از DB می‌سازد و چیزی را متوقف نمی‌کند.
"""
import logging
import uuid

from django.core.cache import cache
from django.urls import reverse
from django.templatetags.static import static
from django.utils import timezone

from products.chat_settings import (
    CHAT_CFG_CACHE_KEY, CHAT_CFG_CACHE_TTL, clamp, current_hours_state, parse_bubble_lines, viewer_allowed, visible_tabs,
)

logger = logging.getLogger(__name__)

# فاز ۲: API پیام آفلاین (chat/api.py) آماده است. گفتگوی زنده در فاز ۳ می‌آید.
CHAT_BACKEND_READY = True
BACKEND_NOT_READY_TEXT = 'ارسال پیام هنوز فعال نشده است؛ به‌زودی در دسترس خواهد بود.'

AVATAR_STATIC = {
    'support': 'theme/assets/chat/avatar-support.svg',
    'character': 'theme/assets/chat/avatar-character.svg',
}


def _avatar(s):
    choice = s.chat_avatar_choice
    if choice == 'custom' and s.chat_avatar_custom:
        try:
            return {'kind': 'custom', 'url': s.chat_avatar_custom.url}
        except ValueError:
            pass                                   # فایل مفقود ← پیش‌فرض
        choice = 'support'
    if choice not in AVATAR_STATIC:
        choice = 'support'
    return {'kind': choice, 'url': static(AVATAR_STATIC[choice])}


def api_urls():
    """ نشانی‌های API برای کلاینت؛ شناسه‌ی گفتگو با «__ID__» جایگزین می‌شود (کلاینت آدرس‌ها را حدس نمی‌زند) """
    zero = uuid.UUID(int=0)

    def with_id(name):
        return reverse(f'chat:{name}', args=[zero]).replace(str(zero), '__ID__')

    return {
        'state': reverse('chat:state'), 'create': reverse('chat:create'), 'messages': with_id('messages'), 'send': with_id('send'),
        'read': with_id('read'), 'close': with_id('close'),
    }


def build_snapshot(s):
    """ بخش ایستای پیکربندی (مستقل از زمان و از بازدیدکننده) """
    try:
        bubble = parse_bubble_lines(s.chat_bubble_messages)
    except ValueError:
        bubble = []
    return {
        'version': 2,
        'api': api_urls(),
        'backend_ready': CHAT_BACKEND_READY,
        'color': s.chat_primary_color,
        'position': {
            'side': s.chat_position if s.chat_position in ('left', 'right') else 'left',
            'x': clamp(s.chat_offset_x_px, 0, 200),
            'y': clamp(s.chat_offset_y_px, 0, 200),
            'x_mobile': None if s.chat_offset_x_px_mobile is None else clamp(s.chat_offset_x_px_mobile, 0, 200),
            'y_mobile': None if s.chat_offset_y_px_mobile is None else clamp(s.chat_offset_y_px_mobile, 0, 200),
        },
        'tabs': [{'key': key, 'label': label, 'coming_soon': soon} for key, label, soon in visible_tabs(s)],
        'texts': {
            'title': s.chat_title,
            'subtitle_online': s.chat_subtitle_online,
            'subtitle_offline': s.chat_subtitle_offline,
            'welcome': s.chat_welcome_message,
            'no_operator': s.chat_msg_no_operator,
            'after_hours': s.chat_msg_after_hours,
            'offline_intro': s.chat_offline_form_intro,
            'offline_success': s.chat_offline_success_message,
            'ai_coming_soon': s.chat_ai_coming_soon_text,
            'privacy': s.chat_privacy_notice,
            'message_placeholder': s.chat_message_placeholder,
            'name_placeholder': s.chat_name_placeholder,
            'phone_placeholder': s.chat_phone_placeholder,
            'send': s.chat_send_label,
            'to_offline': s.chat_to_offline_label,
            'close_conversation': s.chat_close_conversation_label,
            'backend_not_ready': BACKEND_NOT_READY_TEXT,
        },
        'avatar': _avatar(s),
        'anim': {
            'enabled': bool(s.chat_anim_enabled),
            'float': bool(s.chat_anim_float),
            'pulse': bool(s.chat_anim_pulse),
            'wave': bool(s.chat_anim_wave),
            'bubble': bool(s.chat_anim_bubble),
            'attention_interval': clamp(s.chat_attention_interval_seconds, 5, 300),
            'respect_reduced_motion': bool(s.chat_respect_reduced_motion),
        },
        'bubble': {
            'messages': bubble,
            'interval': clamp(s.chat_bubble_interval_seconds, 3, 120),
            'first_delay': clamp(s.chat_bubble_first_delay_seconds, 0, 60),
        },
        'dismiss_hours': clamp(s.chat_launcher_dismiss_hours, 0, 720),
        'guest_form': {'name': s.chat_guest_name_mode, 'phone': s.chat_guest_phone_mode},
        'limits': {'message_max_length': clamp(s.chat_message_max_length, 50, 4000)},
        'poll': {
            'active': clamp(s.chat_poll_active_seconds, 2, 30),
            'idle': clamp(s.chat_poll_idle_seconds, 2, 120),
            'closed': 0 if not s.chat_poll_closed_seconds else clamp(s.chat_poll_closed_seconds, 15, 600),
        },
    }


def get_snapshot(s):
    try:
        snapshot = cache.get(CHAT_CFG_CACHE_KEY)
    except Exception:  # noqa: BLE001 - Redis قطع است؛ از DB می‌سازیم
        snapshot = None
    if snapshot is None:
        snapshot = build_snapshot(s)
        try:
            cache.set(CHAT_CFG_CACHE_KEY, snapshot, CHAT_CFG_CACHE_TTL)
        except Exception:  # noqa: BLE001
            logger.warning('ذخیره‌ی snapshot پیکربندی چت در کش ناموفق بود.')
    return snapshot


def availability(hours):
    """
    وضعیت دسترسی گفتگوی زنده. فاز ۱: هنوز حضور کارشناس (Presence) وجود ندارد، پس گفتگوی زنده همیشه ناموجود است:
      خارج از ساعت کاری ← 'after_hours'؛ در ساعت کاری ← 'no_operator'.
    در فاز ۳ وضعیت ۱ (ساعت کاری + کارشناس آنلاین ← 'live') هم اضافه می‌شود.
    """
    state = 'no_operator' if hours['in_hours'] else 'after_hours'
    return {'live': False, 'state': state}


def public_config(s, *, is_authenticated, now=None):
    """ پاسخ کامل GET /chat/config/ برای این بازدیدکننده؛ {'enabled': False} اگر نباید ویجت ببیند """
    if not s.chat_enabled or not viewer_allowed(s, is_authenticated) or not visible_tabs(s):
        return {'enabled': False}
    hours = current_hours_state(s, now or timezone.now())
    payload = dict(get_snapshot(s))
    payload.update({
        'enabled': True,
        'hours': {
            'mode': s.chat_hours_mode, 'in_hours': hours['in_hours'], 'today_label': hours['today_label'],
            'next_open_label': hours['next_open_label'],
        },
        'availability': availability(hours),
        'viewer': {'authenticated': bool(is_authenticated)},
    })
    return payload
