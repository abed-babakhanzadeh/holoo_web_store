"""
حضور کارشناسان (Presence). کارشناس در پیشخوان خود را «آنلاین» اعلام می‌کند (کلید دستی is_online) و صفحه‌ی پیشخوان هر چند ثانیه نبض
می‌فرستد (last_seen). «در دسترس» یعنی: کلید روشن و آخرین نبض از chat_operator_timeout_seconds قدیمی‌تر نباشد. دیتابیس مرجع است؛
نتیجه‌ی «آیا کسی در دسترس است؟» فقط ۵ ثانیه در Redis کش می‌شود تا هر بازدیدکننده‌ای یک کوئری ایجاد نکند (قطع Redis ← مستقیم DB).

حضور هرگز وضعیت گفتگوهای موجود را عوض نمی‌کند؛ فقط (۱) دروازه‌ی شروع گفتگوی زنده (chat/availability.py) و (۲) جاروی تایمرها که
گفتگوی ACTIVE کارشناسِ غایب را به صف برمی‌گرداند (chat/timers.py) از آن می‌پرسند.
"""
from datetime import timedelta

from django.db import IntegrityError
from django.utils import timezone

from . import cache as chatcache
from .models import OperatorPresence
from .settingsio import cfg as load_cfg

AVAILABLE_CACHE_KEY = 'presence:any'
AVAILABLE_CACHE_TTL = 5


def _timeout(cfg):
    return timedelta(seconds=int(cfg.chat_operator_timeout_seconds or 60))


def fresh_rows(cfg, now):
    return OperatorPresence.objects.filter(
        is_online=True, last_seen__gte=now - _timeout(cfg), operator__is_active=True, operator__is_staff=True)


def online_count(cfg=None, now=None):
    cfg = cfg or load_cfg()
    return fresh_rows(cfg, now or timezone.now()).count()


def operator_available(now=None, cfg=None):
    """ آیا دست‌کم یک کارشناس آنلاین (با نبض تازه) هست؟ (کش ۵ ثانیه‌ای، قطع Redis ← DB) """
    cached = chatcache.get(AVAILABLE_CACHE_KEY)
    if cached is not None:
        return bool(cached)
    cfg = cfg or load_cfg()
    value = fresh_rows(cfg, now or timezone.now()).exists()
    chatcache.put(AVAILABLE_CACHE_KEY, 1 if value else 0, AVAILABLE_CACHE_TTL)
    return value


def get_or_create_presence(user):
    try:
        presence, _ = OperatorPresence.objects.get_or_create(operator=user)
    except IntegrityError:                                    # دو درخواست هم‌زمان برای کارشناس تازه
        presence = OperatorPresence.objects.get(operator=user)
    return presence


def heartbeat(user, *, online=None, now=None):
    """
    نبض کارشناس. online=None فقط last_seen را به‌روز می‌کند (نبض)؛ True/False کلید دستی را هم عوض می‌کند. ← ردیف حضور.
    نبض کلید را روشن نمی‌کند: کارشناسی که خودش آفلاین شده با نبض خودکار صفحه دوباره آنلاین نمی‌شود.
    """
    now = now or timezone.now()
    presence = get_or_create_presence(user)
    was_fresh = bool(presence.is_online and presence.last_seen and presence.last_seen >= now - _timeout(load_cfg()))
    fields = {'last_seen': now}
    if online is not None:
        fields['is_online'] = bool(online)
    OperatorPresence.objects.filter(pk=presence.pk).update(**fields)
    if (online is not None and bool(online) != presence.is_online) or (presence.is_online and not was_fresh):
        chatcache.forget(AVAILABLE_CACHE_KEY)                 # تغییر کلید دستی یا بازگشت از غیبت باید فوراً دیده شود
    presence.refresh_from_db()
    return presence


def present_operator_ids(cfg, now, operator_ids):
    """ از میان operator_idها آن‌هایی که الان آنلاین‌اند (برای جاروی غیبت مسئول) """
    ids = list(operator_ids)
    if not ids:
        return set()
    return set(fresh_rows(cfg, now).filter(operator_id__in=ids).values_list('operator_id', flat=True))
